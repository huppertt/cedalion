from collections import defaultdict

import numpy as np
import xarray as xr
#from nilearn.glm.first_level import run_glm as nilearn_run_glm

import cedalion.typing as cdt
import cedalion.xrutils as xrutils
import cedalion.dataclasses.statistics
import statsmodels.regression
import statsmodels.api
import pandas as pd
from scipy.linalg import toeplitz

from pqdm.processes import pqdm
from tqdm import tqdm
import cedalion.math.ar_irls

def _hash_channel_wise_regressor(regressor: xr.DataArray) -> list[int]:
    """Hashes each channel slice of the regressor array.

    Args:
        regressor: array of channel-wise regressors. Dims
            (channel, regressor, time, chromo|wavelength)

    Returns:
        A list of hash values, one hash for each channel.
    """

    tmp = regressor.pint.dequantify()
    n_channel = regressor.sizes["channel"]
    return [hash(tmp.isel(channel=i).values.data.tobytes()) for i in range(n_channel)]


def _channel_fit(y,x,noise_model='ols',ar_order=30):
    if noise_model=='ols':
        ss=statsmodels.api.OLS(y,x).fit()
    elif noise_model=='rls':
        ss=statsmodels.api.RecursiveLS(y,x).fit()
    elif noise_model=='wls':
        ss=statsmodels.api.WLS(y,x).fit()
    elif noise_model=='ar_irls':
        ss=cedalion.math.ar_irls.ar_irls_GLM(y,x,pmax=ar_order)
    elif noise_model=='gls':
        ols_resid=statsmodels.api.OLS(y,x).fit().resid
        resid_fit=statsmodels.api.OLS(
            np.asarray(ols_resid[1:]),statsmodels.api.add_constant(np.asarray(ols_resid)[:-1])
            ).fit()
        rho=resid_fit.params[1]
        order=toeplitz(range(len(ols_resid)))
        ss=statsmodels.api.GLS(y,x,sigma=rho**order).fit()
    elif noise_model=='glsar':
        ss=statsmodels.api.GLSAR(y,x,ar_order).iterative_fit(4)

    else:
        NotImplemented

    return ss


def fit(
    ts: cdt.NDTimeSeries,
    design_matrix: xr.DataArray,
    channel_wise_regressors: list[xr.DataArray] | None = None,
    noise_model="ols",ar_order=30,max_jobs=3,verbose=False
):
    """Fit design matrix to data.

    Args:
        ts: the time series to be modeled
        design_matrix: DataArray with dims time, regressor, chromo
        channel_wise_regressors: optional list of design matrices, with additional
            channel dimension
        noise_model: must be 'ols' for the moment

    Returns:
        thetas as a DataArray

    """
    #if noise_model != "ols":
    #    raise NotImplementedError("support for other noise models is missing")

    # FIXME: unit handling?
    # shoud the design matrix be dimensionless? -> thetas will have units
    ts = ts.pint.dequantify()
    design_matrix = design_matrix.pint.dequantify()

    dim3_name = xrutils.other_dim(design_matrix, "time", "regressor")

    df = pd.DataFrame()

    resid = [] 

    for dim3, group_channels, group_design_matrix in iter_design_matrix(ts, design_matrix, channel_wise_regressors):
        group_y = ts.sel({"channel": group_channels, dim3_name: dim3}).transpose("time", "channel")
            
        x=pd.DataFrame(group_design_matrix.values)
        x.columns=group_design_matrix.regressor

        y=pd.DataFrame(group_y.values)
        y.columns=group_y.channel

        stats=[]

        if(max_jobs==1):
            if(verbose):
                for chan in tqdm(group_y.channel.values):
                    ss=_channel_fit(y[chan],x,noise_model,ar_order)
                    stats.append(ss)
                    resid.append(ss.resid.to_numpy())
            else:
                for chan in group_y.channel.values:
                    ss=_channel_fit(y[chan],x,noise_model,ar_order)
                    stats.append(ss)
                    resid.append(ss.resid.to_numpy())
        else:
            args_list=[]
            for chan in group_y.channel.values:
                args_list.append([y[chan],x,noise_model,ar_order])        

            results = pqdm(args_list, _channel_fit, n_jobs=max_jobs, argument_type="args")

            for ss in results:
                stats.append(ss)
                resid.append(ss.resid.to_numpy())

        df = pd.concat([df,pd.DataFrame({'channel':group_y.channel,'type':dim3,'models':stats})],
                       ignore_index=True,axis=0)

    coloring_matrix=np.linalg.cholesky(np.corrcoef(np.array(resid)))
    coloring_matrix=xr.DataArray(data=coloring_matrix,dims=['channel','type'],coords={'channel':df.channel,'type':df.type})

    stats = cedalion.dataclasses.statistics.Statistics()
    
    if noise_model=='ols':
        stats.description='OLS model via statsmodels.regression'
    elif noise_model=='rls':
        stats.description='Recursive LS model via statsmodels.regression'
    elif noise_model=='gls':
        stats.description='Generalized LS model via statsmodels.regression'
    elif noise_model=='glsar':
        stats.description='Generalized LS AR-model via statsmodels.regression'

    stats.models=df
    stats.coloring_matrix=coloring_matrix
   
    return stats


def predict(
    ts: cdt.NDTimeSeries,
    thetas: xr.DataArray,
    design_matrix: xr.DataArray,
    channel_wise_regressors: list[xr.DataArray] | None = None,
) -> cdt.NDTimeSeries:
    """Predict time series from design matrix and thetas.

    Args:
        ts (cdt.NDTimeSeries): The time series to be modeled.
        thetas (xr.DataArray): The estimated parameters.
        design_matrix (xr.DataArray): DataArray with dims time, regressor, chromo
        channel_wise_regressors (list[xr.DataArray]): Optional list of design matrices,
        with additional channel dimension.

    Returns:
        prediction (xr.DataArray): The predicted time series.
    """
    dim3_name = xrutils.other_dim(design_matrix, "time", "regressor")

    prediction = defaultdict(list)

    for dim3, group_channels, group_design_matrix in iter_design_matrix(
        ts, design_matrix, channel_wise_regressors
    ):
        # (dim3, channel, regressor)
        t = thetas.sel({"channel": group_channels, dim3_name: [dim3]})
        prediction[dim3].append(xr.dot(group_design_matrix, t, dim="regressor"))

        """
        tmp = xr.dot(group_design_matrix, t, dim="regressor")

        # Drop coordinates that are in group_design_matrix but which we don't want
        # to be in the predicted time series. This is currently formulated as a
        # negative list. A positive list might be the better choice, though.
        tmp = tmp.drop_vars(
            [i for i in ["short_channel", "comp_group"] if i in tmp.coords]
        )

        prediction[dim3].append(tmp)
        """

    # concatenate channels
    prediction = [xr.concat(v, dim="channel") for v in prediction.values()]

    # concatenate dim3
    prediction = xr.concat(prediction, dim=dim3_name)

    return prediction


def iter_design_matrix(
    ts: cdt.NDTimeSeries,
    design_matrix: xr.DataArray,
    channel_wise_regressors: list[xr.DataArray] | None = None,
    channel_groups: list[int] | None = None,
):
    """Iterate over the design matrix and yield the design matrix for each group.

    Args:
        ts (cdt.NDTimeSeries): The time series to be modeled.
        design_matrix (xr.DataArray): DataArray with dims time, regressor, chromo.
        channel_wise_regressors (list[xr.DataArray] | None, optional): Optional list of
            design matrices, with additional channel dimension.
        channel_groups (list[int] | None, optional): Optional list of channel groups.

    Yields:
        tuple: A tuple containing:
            - dim3 (str): The third dimension name.
            - group_y (cdt.NDTimeSeries): The grouped time series.
            - group_design_matrix (xr.DataArray): The grouped design matrix.
    """
    dim3_name = xrutils.other_dim(design_matrix, "time", "regressor")

    if channel_wise_regressors is None:
        channel_wise_regressors = []

    for cwreg in channel_wise_regressors:
        assert cwreg.sizes["regressor"] == 1
        assert (ts.channel.values == cwreg.channel.values).all()

    comp_groups = []
    for reg in channel_wise_regressors:
        if "comp_group" in reg.coords:
            comp_groups.append(reg["comp_group"].values)
        else:
            comp_groups.append(_hash_channel_wise_regressor(reg))

    if channel_groups is not None:
        assert len(channel_groups) == ts.sizes["channel"]
        comp_groups.append(channel_groups)

    if len(comp_groups) == 0:
        # There are no channel-wise regressors. Just iterate over the third dimension
        # of the design matrix.
        for dim3 in design_matrix[dim3_name].values:
            dm = design_matrix.sel({dim3_name: dim3})
            # group_y = ts.sel({dim3_name: dim3})
            channels = ts.channel.values
            # yield dim3, group_y, dm
            yield dim3, channels, dm

        return
    else:
        # there are channel-wise regressors. For each computational group, in which
        # the channel-wise regressors are identical, we have to assemble and yield the
        # design-matrix.

        chan_idx_with_same_comp_group = defaultdict(list)

        for i_ch, all_comp_groups in enumerate(zip(*comp_groups)):
            chan_idx_with_same_comp_group[all_comp_groups].append(i_ch)

        for dim3 in design_matrix[dim3_name].values:
            dm = design_matrix.sel({dim3_name: dim3})

            for chan_indices in chan_idx_with_same_comp_group.values():
                channels = ts.channel[np.asarray(chan_indices)].values

                regs = []
                for reg in channel_wise_regressors:
                    regs.append(
                        reg.sel({"channel": channels, dim3_name: dim3})
                        .isel(channel=0)  # regs are identical within a group
                        .pint.dequantify()
                    )

                group_design_matrix = xr.concat([dm] + regs, dim="regressor")
                # group_y = ts.sel({"channel": channels, dim3_name: dim3})

                # yield dim3, group_y, group_design_matrix
                yield dim3, channels, group_design_matrix
