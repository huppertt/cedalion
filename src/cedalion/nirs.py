import numpy as np
import xarray as xr
from numpy.typing import ArrayLike
from scipy.interpolate import interp1d
from cedalion import units

import cedalion
import cedalion.typing as cdt
import cedalion.validators as validators
import cedalion.xrutils as xrutils
import cedalion.data


def get_extinction_coefficients(spectrum: str, wavelengths: ArrayLike):
    """Provide a matrix of extinction coefficients from tabulated data.

    Args:
        spectrum: The type of spectrum to use. Currently supported options are:
            - "prahl": Extinction coefficients based on the Prahl absorption spectrum
                       (Prahl1998).
        wavelengths: An array-like object containing the wavelengths at which to
            calculate the extinction coefficients.

    Returns:
        xr.DataArray: A matrix of extinction coefficients with dimensions "chromo"
            (chromophore, e.g. HbO/HbR) and "wavelength" (e.g. 750, 850, ...) at which
            the coefficients for each chromophore are given in units of "mm^-1 / M".

    References:
        (Prahl 1998) - taken from Homer2/3, Copyright 2004 - 2006 - The General Hospital
            Corporation and President and Fellows of Harvard University.
            "These values for the molar extinction coefficient e in [cm-1/(moles/liter)]
            were compiled by Scott Prahl (prahl@ece.ogi.edu) using data from
            W. B. Gratzer, Med. Res. Council Labs, Holly Hill, London
            N. Kollias, Wellman Laboratories, Harvard Medical School, Boston
            To convert this data to absorbance A, multiply by the molar concentration
            and the pathlength.
            For example, if x is the number of grams per liter and a 1 cm cuvette is
            being used, then the absorbance is given by
            (e) [(1/cm)/(moles/liter)] (x) [g/liter] (1) [cm]
            A =  ---------------------------------------------------
                        66,500 [g/mole]
            using 66,500 as the gram molecular weight of hemoglobin.
            To convert this data to absorption coefficient in (cm-1), multiply by the
            molar concentration and 2.303,
            µa = (2.303) e (x g/liter)/(66,500 g Hb/mole)
            where x is the number of grams per liter. A typical value of x for whole
            blood is x=150 g Hb/liter."
    """
    if spectrum == "prahl":
        path = cedalion.data.get("prahl_absorption_spectrum.tsv")
        with path.open("r") as fin:
            coeffs = np.loadtxt(fin, comments="#")

        chromophores = ["HbO", "HbR"]
        spectra = [
            interp1d(coeffs[:, 0], np.log(10) * coeffs[:, i] / 10) for i in [1, 2]
        ]  # convert units from cm^-1/ M to mm^-1 / M

        E = np.array([spec(wl) for spec in spectra for wl in wavelengths]).reshape(
            len(spectra), len(wavelengths)
        )

        E = xr.DataArray(
            E,
            dims=["chromo", "wavelength"],
            coords={"chromo": chromophores, "wavelength": wavelengths},
            attrs={"units": "mm^-1 / M"},
        )
        E = E.pint.quantify()
        return E
    else:
        raise ValueError(f"unsupported spectrum '{spectrum}'")


def channel_distances(amplitudes: xr.DataArray, geo3d: xr.DataArray):
    """Calculate distances between channels.

    Args:
        amplitudes (xr.DataArray): A DataArray representing the amplitudes with
            dimensions (channel, *).
        geo3d (xr.DataArray): A DataArray containing the 3D coordinates of the channels
            with dimensions (channel, pos).

    Returns:
        dists (xr.DataArray): A DataArray containing the calculated distances between
            source and detector channels. The resulting DataArray has the dimension
            'channel'.
    """
    validators.has_channel(amplitudes)
    validators.has_positions(geo3d, npos=3)
    validators.is_quantified(geo3d)

    diff = geo3d.loc[amplitudes.source] - geo3d.loc[amplitudes.detector]
    dists = xrutils.norm(diff, geo3d.points.crs)
    dists = dists.rename("dists")

    return dists


def int2od(amplitudes: xr.DataArray):
    """Calculate optical density from intensity amplitude  data.

    Args:
        amplitudes (xr.DataArray, (time, channel, *)): amplitude data.

    Returns:
        od: (xr.DataArray, (time, channel,*): The optical density data.
    """
    # check negative values in amplitudes and issue an error if yes
    if np.any(amplitudes < 0):
        raise AssertionError(
            "Error: DataArray contains negative values. Please fix, for example by "
            "setting them to NaN with "
            "'amplitudes = amplitudes.where(amplitudes >= 0, np.nan)'"
        )

    # conversion to optical density
    od = -np.log(amplitudes / amplitudes.mean("time"))
    return od

def od2int(od: xr.DataArray):
    """Calculate intensity amplitude from  optical density data.

    Args:
        od (xr.DataArray, (time, channel, *)): od data.

    Returns:
        amplitudes: (xr.DataArray, (time, channel,*): "raw" intensity data.
    """

    # conversion to optical density
    amplitudes  = np.abs(np.exp(-od)*100)
    
    return amplitudes 




def od2conc(
    od: xr.DataArray,
    geo3d: xr.DataArray,
    dpf: xr.DataArray,
    spectrum: str = "prahl",
):
    """Calculate concentration changes from optical density data.

    Args:
        od (xr.DataArray, (channel, wavelength, *)): The optical density data array
        geo3d (xr.DataArray): The 3D coordinates of the optodes.
        dpf (xr.DataArray, (wavelength, *)): The differential pathlength factor data
        spectrum (str, optional): The type of spectrum to use for calculating extinction
            coefficients. Defaults to "prahl".

    Returns:
        conc (xr.DataArray, (channel, *)): A data array containing
            concentration changes by channel.
    """
    validators.has_channel(od)
    validators.has_wavelengths(od)
    validators.has_wavelengths(dpf)
    validators.has_positions(geo3d, npos=3)

    E = get_extinction_coefficients(spectrum, od.wavelength)

    Einv = xrutils.pinv(E)

    dists = channel_distances(od, geo3d)
    dists = dists.pint.to("mm")

    # conc = Einv @ (optical_density / ( dists * dpf))
    if dpf[0] != 1:
        conc = xr.dot(Einv, od / (dists * dpf), dims=["wavelength"])
    else:
        conc = xr.dot(Einv, od / (dpf * 1*units.mm), dims=["wavelength"])

    conc = conc.pint.to("micromolar")
    conc = conc.pint.quantify({"time": od.time.attrs["units"]})  # got lost in xr.dot
    conc = conc.rename("concentration")

    return conc


def conc2od(
    conc: xr.DataArray,
    geo3d: xr.DataArray,
    dpf: xr.DataArray,
    spectrum: str = "prahl",
):
    """Calculate concentration changes from optical density data.

    Args:
        conc (xr.DataArray, (channel, *)): A data array containing
            concentration changes by channel.
        
        geo3d (xr.DataArray): The 3D coordinates of the optodes.
        dpf (xr.DataArray, (wavelength, *)): The differential pathlength factor data
        spectrum (str, optional): The type of spectrum to use for calculating extinction
            coefficients. Defaults to "prahl".

    Returns:
        od (xr.DataArray, (channel, wavelength, *)): The optical density data array
    """

    validators.has_channel(conc)
    validators.has_wavelengths(dpf)
    validators.has_positions(geo3d, npos=3)

    conc = conc.pint.to("molar")
    conc = conc.pint.dequantify()

    E = get_extinction_coefficients(spectrum, dpf.wavelength)

    #Einv = xrutils.pinv(E)

    dists = channel_distances(conc, geo3d)
    dists = dists.pint.to("mm")

    # conc = Einv @ (optical_density / ( dists * dpf))
    # od = E @ conc @ (dist * dpf)
    if dpf[0] != 1:
        od = xr.dot(E, conc * (dists * dpf), dims=["chromo"])
    else:
        od = xr.dot(E, conc * (dpf * 1*units.mm), dims=["chromo"])

    od.data = od.pint.dequantify()
    od = od.pint.quantify({"time": conc.time.attrs["units"]})  # got lost in xr.dot
    od = od.rename("optical density")

    return od



def beer_lambert(
    amplitudes: xr.DataArray,
    geo3d: xr.DataArray,
    dpf: xr.DataArray,
    spectrum: str = "prahl",
):
    """Calculate concentration changes from amplitude using the modified BL law.

    Args:
        amplitudes (xr.DataArray, (channel, wavelength, *)): The input data array
            containing the raw intensities.
        geo3d (xr.DataArray): The 3D coordinates of the optodes.
        dpf (xr.DataArray, (wavelength,*)): The differential pathlength factors
        spectrum (str, optional): The type of spectrum to use for calculating extinction
            coefficients. Defaults to "prahl".

    Returns:
        conc (xr.DataArray, (channel, *)): A data array containing
            concentration changes according to the mBLL.
    """
    validators.has_channel(amplitudes)
    validators.has_wavelengths(amplitudes)
    validators.has_wavelengths(dpf)
    validators.has_positions(geo3d, npos=3)

    # calculate optical densities
    od = int2od(amplitudes)
    # calculate concentrations
    conc = od2conc(od, geo3d, dpf, spectrum)

    return conc


def split_long_short_channels(
    ts: cdt.NDTimeSeries,
    geo3d: cdt.LabeledPointCloud,
    distance_threshold: cedalion.Quantity = 1.5 * cedalion.units.cm,
):
    """Split a time series into two based on channel distances.

    Args:
        ts (cdt.NDTimeSeries) : Time series to split.
        geo3d (cdt.LabeledPointCloud) : 3D coordinates of the channels.
        distance_threshold (Quantity) : Distance threshold for splitting the channels.

    Returns:
        ts_long : time series with channel distances >= distance_threshold
        ts_short : time series with channel distances < distance_threshold
    """
    dists = xrutils.norm(
        geo3d.loc[ts.source] - geo3d.loc[ts.detector], dim=geo3d.points.crs
    )

    mask = dists < distance_threshold
    ts_short = ts.sel(channel=mask)
    ts_long = ts.sel(channel=~mask)

    return ts_long, ts_short
