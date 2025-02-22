"""
Compute experimental OTF of microscope, based on a series of DMD pictures taken a range of angles/pattern spacings.

Using known affine transformation as a starting point, identify peaks in fluorescence image. These must be normalized
to the DC component of the fluorescence image (laser intensity may change between frames) and to the size of the fourier
component at the frequency in the given pattern.

Actually, we need to the fourier transform of the intensity for our DMD pattern properly bandlimited for the effect
of the imaging system on the coherent light.
"""

import time
import numpy as np
from scipy import fft
import scipy.signal
import scipy.optimize

import matplotlib.colors
from matplotlib.colors import PowerNorm
import matplotlib.patches
import matplotlib.pyplot as plt

from mcsim.analysis import dmd_patterns, simulate_dmd, sim_reconstruction
import mcsim.analysis.analysis_tools as tools
from localize_psf import affine, fit_psf


def frq2angles(frq_2d,
               wavelength,
               n):
    """
    Convert from frequency vectors (in angular spectrum representation) to angles

    f = n/lambda * [cos(phi)*sin(theta), sin(phi)*sin(theta), cos(theta)]

    :param frq_2d:
    :param wavelength:
    :param n:

    :return phi:
    :return theta:
    """
    frq_2d = np.array(frq_2d, copy=True)

    # phi = np.arctan(wavelength / n * frq_2d[..., 1] / frq_2d[..., 0])
    # phi[frq_2d[..., 0] == 0] = np.pi/2
    phi = np.atleast_1d(np.angle(frq_2d[..., 0] + 1j * frq_2d[..., 1]))
    theta = np.atleast_1d(np.arcsin(wavelength / n * np.linalg.norm(frq_2d, axis=-1)))

    # ensure disallowed points return nans
    disallowed = np.linalg.norm(frq_2d, axis=-1) > n/wavelength
    phi[disallowed] = np.nan
    theta[disallowed] = np.nan

    return phi, theta


def angles2frq(theta,
               phi,
               wavelength,
               n):
    """

    :param theta:
    :param phi:
    :param float wavelength:
    :param float n: index of refraction of medium

    :return frq:
    """
    frq = n/wavelength * np.array([np.cos(phi) * np.sin(theta),
                                   np.sin(phi) * np.sin(theta),
                                   np.cos(theta)])
    return frq


def interfere_polarized(theta1,
                        phi1,
                        theta2,
                        phi2,
                        alpha):
    """
    Let the optical axis point along z. theta is the angle with respect to the optical axis,
    and phi is the azimuthal angle of the ray. Alpha is the azimuthal angle of the polarization,
    which we assume is initially orthogonal to the optical axis

    The polarization vector is
    p = [np.cos(alpha), np.sin(alpha), 0]
    The s/p unit vectors are
    ep = [np.cos(phi), np.sin(phi), 0]
    es = [-np.sin(phi), np.cos(phi), 0]
    after refracting through the lens, the polarization vector is
    pr = (p.dot(ep)) * er + (p.odt(es)) * es
    with er now pointing orthothogonal to the propogation of the refracted ray
    er = [np.cos(phi) * np.cos(theta), np.sin(phi) * np.cos(theta), np.sin(theta)]

    This function compute pr(theta1, phi1, alpha).dot(pr(theta2, phi2, alpha))

    :param theta1: angle of first ray
    :param phi1: azimuthal angle of first ray
    :param theta2: angle of second ray
    :param phi2: azimuthal angle of second ray
    :param alpha: polarization angle

    :return: dot product of the two polarization vectors
    """
    int = np.cos(alpha - phi1) * np.cos(alpha - phi2) * np.cos(phi1) * np.cos(phi2) * np.cos(theta1) * np.cos(theta2) + \
        np.sin(alpha - phi1) * np.sin(alpha - phi2) * np.sin(phi1) * np.sin(phi2) + \
        -np.cos(alpha - phi1) * np.sin(alpha - phi2) * np.cos(phi1) * np.sin(phi2) * np.cos(theta1) + \
        -np.sin(alpha - phi1) * np.cos(alpha - phi2) * np.sin(phi1) * np.cos(phi2) * np.cos(theta2) + \
        np.cos(alpha - phi1) * np.cos(alpha - phi2) * np.sin(phi1) * np.sin(phi2) * np.cos(theta1) * np.cos(theta2) + \
        np.sin(alpha - phi1) * np.sin(alpha - phi2) * np.cos(phi1) * np.cos(phi2) + \
        np.cos(alpha - phi1) * np.sin(alpha - phi2) * np.sin(phi1) * np.cos(phi2) * np.cos(theta1) + \
        np.sin(alpha - phi1) * np.cos(alpha - phi2) * np.cos(phi1) * np.sin(phi2) * np.cos(theta2) + \
        np.cos(alpha - phi1) * np.cos(alpha - phi2) * np.sin(theta1) * np.sin(theta2)

    return int


def interfere_unpolarized(theta1,
                          phi1,
                          theta2,
                          phi2):
    """
    Intereference averaged over input polarization
    :param theta1:
    :param phi1:
    :param theta2:
    :param phi2:
    :return:
    """
    intensity = 0.5 * np.cos(phi1 - phi2)**2 * (1 + np.cos(theta1) * np.cos(theta2)) + \
                0.5 * np.cos(phi1 - phi2) * np.sin(theta1) * np.sin(theta2) + \
                0.5 * np.sin(phi1 - phi2)**2 * (np.cos(theta1) + np.cos(theta2))

    return intensity


def get_int_fc(efield_fc):
    """
    Generate intensity fourier components from efield fourier components ignoring polarization effects

    :param efield_fc: electric field Fourier components nvec1 x nvec2 array,
     where efield_fc[ii, jj] is the electric field at frequencies f = ii * v1 + jj * v2.

    :return intensity_fc: intensity Fourier components at the same frequencies, f = ii * v1 + jj * v2
    """
    ny, nx = efield_fc.shape
    if np.mod(ny, 2) == 0 or np.mod(nx, 2) == 0:
        raise ValueError("not implemented for even sized arrays")

    intensity_fc = scipy.signal.fftconvolve(efield_fc, np.flip(efield_fc, axis=(0, 1)).conj(), mode='same')

    return intensity_fc


def get_int_fc_pol(efield_fc,
                   vecs,
                   wavelength,
                   n,
                   polarization_angle=None):
    """
    Calculate intensity from electric field including effect of polarization.

    :param efield_fc: electric field Fourier components nvec1 x nvec2 array, where efield_fc[ii, jj]
     is the electric field at frequencies f = ii * v1 + jj * v2.
    :param vecs: nvec1 x nvec2 x 2
    :param float wavelength: wavelength of excitation light. If vecs are given in 1/unit, then wavelength must be given
    in unit.
    :param n: index of refraction of medium
    :param polarization_angle: can either be an angle in radians, or "None" in which case assume unpolarized light.

    :return intensity_fc: nvec1 x nvec2 x 2
    """
    ny, nx = efield_fc.shape
    if np.mod(ny, 2) == 0 or np.mod(nx, 2) == 0:
        raise ValueError("not implemented for even sized arrays")

    # define convolution
    def conv(efield): return scipy.signal.fftconvolve(efield, np.flip(efield, axis=(0, 1)).conj(), mode='same')

    # convert frequencies in distance units to polar angles
    phis, thetas = frq2angles(vecs, wavelength, n)

    if polarization_angle is None:
        # these expressions can be arrived at by factoring those found in interfere_unpolarized()
        # see also eq. 9 in https://doi.org/10.1364/OE.22.011140
        polx_a = np.sin(phis)**2 + np.cos(thetas) * np.cos(phis)**2
        poly_a = np.sin(phis) * np.cos(phis) * (np.cos(thetas) - 1)
        polz_a = np.cos(phis) * np.sin(thetas)
        polx_b = np.sin(phis) * np.cos(phis) * (np.cos(thetas) - 1)
        poly_b = np.cos(phis)**2 + np.cos(thetas) * np.sin(phis)**2
        polz_b = np.sin(phis) * np.sin(thetas)

        polx_a[np.isnan(polx_a)] = 0
        poly_a[np.isnan(poly_a)] = 0
        polz_a[np.isnan(polz_a)] = 0
        polx_b[np.isnan(polx_b)] = 0
        poly_b[np.isnan(poly_b)] = 0
        polz_b[np.isnan(polz_b)] = 0

        intensity_fc = 0.5 * (conv(efield_fc * polx_a) + conv(efield_fc * poly_a) + conv(efield_fc * polz_a) +
                              conv(efield_fc * polx_b) + conv(efield_fc * poly_b) + conv(efield_fc * polz_b))
    else:
        # similar to above, but not 0.5 and simpler factorization because no averaging
        polx = np.cos(polarization_angle - phis) * np.cos(phis) * np.cos(thetas) - np.sin(polarization_angle - phis) * np.sin(phis)
        poly = np.cos(polarization_angle - phis) * np.sin(phis) * np.cos(thetas) + np.sin(polarization_angle - phis) * np.cos(phis)
        polz = np.cos(polarization_angle - phis) * np.sin(thetas)

        polx[np.isnan(polx)] = 0
        poly[np.isnan(poly)] = 0
        polz[np.isnan(polz)] = 0

        intensity_fc = conv(efield_fc * polx) + conv(efield_fc * poly) + conv(efield_fc * polz)

    return intensity_fc


def get_all_fourier_exp(imgs,
                        frq_vects_theory,
                        roi: list[int],
                        pixel_size_um: float,
                        fmax_img,
                        to_use=None,
                        use_guess_frqs: bool = True,
                        max_frq_shift_pix: float = 1.5,
                        force_start_from_guess: bool = True,
                        peak_pix: int = 2,
                        bg: float = 100):
    """
    Calculate Fourier components from a set of images.

    :param imgs: nimgs x ny x nx
    :param frq_vects_theory: nimgs x nvecs1 x nvecs2 x 2
    :param roi:
    :param pixel_size_um:
    :param fmax_img:
    :param to_use:
    :param use_guess_frqs: if True, use guess frequencies computed from frq_vects_theory, if False use fitting
    procedure to find peak
    :param max_frq_shift_pix:
    :param force_start_from_guess:
    :param int peak_pix: number of pixels to use when calculating peak. Typically 2.
    :param float bg:

    :return intensity:
    :return intensity_unc:
    :return frq_vects_expt:
    """
    if to_use is None:
        to_use = np.ones(frq_vects_theory[:, :, :, 0].shape, dtype=np.int)

    nimgs = frq_vects_theory.shape[0]
    n1_vecs = frq_vects_theory.shape[1]
    n2_vecs = frq_vects_theory.shape[2]

    intensity = np.zeros(frq_vects_theory.shape[:-1], dtype=np.complex) * np.nan
    intensity_unc = np.zeros(intensity.shape) * np.nan

    # apodization, 2D window from broadcasting
    nx_roi = roi[3] - roi[2]
    ny_roi = roi[1] - roi[0]
    window = scipy.signal.windows.hann(nx_roi)[None, :] * scipy.signal.windows.hann(ny_roi)[:, None]

    # generate frequency data for image FT's
    # fxs = tools.get_fft_frqs(nx_roi, pixel_size_um)
    fxs = fft.fftshift(fft.fftfreq(nx_roi, pixel_size_um))
    dfx = fxs[1] - fxs[0]
    # fys = tools.get_fft_frqs(ny_roi, pixel_size_um)
    fys = fft.fftshift(fft.fftfreq(ny_roi, pixel_size_um))
    dfy = fys[1] - fys[0]

    if imgs.shape[0] == nimgs:
        multiple_images = True
    elif imgs.shape[0] == 1:
        multiple_images = False
        icrop = imgs[0, roi[0]:roi[1], roi[2]:roi[3]]

        img = icrop - bg
        img[img < 0] = 1e-6

        img_ft = fft.fftshift(fft.fft2(fft.ifftshift(img * window)))
        noise_power = sim_reconstruction.get_noise_power(img_ft, fxs, fys, fmax_img)
    else:
        raise Exception()

    frq_vects_expt = np.zeros(frq_vects_theory.shape)
    tstart = time.process_time()
    for ii in range(nimgs):
        tnow = time.process_time()
        print("%d/%d, %d peaks, elapsed time = %0.2fs" % (ii + 1, nimgs, np.sum(to_use[ii]), tnow - tstart))

        if multiple_images:
            # subtract background and crop to ROI
            # img = img[0, roi[0]:roi[1], roi[2]:roi[3]] - bg
            # img[img < 0] = 1e-6
            icrop = imgs[ii, roi[0]:roi[1], roi[2]:roi[3]]
            img = icrop - bg
            img[img < 0] = 1e-6

            # fft
            img_ft = fft.fftshift(fft.fft2(fft.ifftshift(img * window)))
            # get noise
            noise_power = sim_reconstruction.get_noise_power(img_ft, fxs, fys, fmax_img)

        # minimimum separation between reciprocal lattice vectors
        vnorms = np.linalg.norm(frq_vects_theory[ii], axis=2)
        min_sep = np.min(vnorms[vnorms > 0])

        # get experimental weights of fourier components
        for aa in range(n1_vecs):
            for bb in range(n2_vecs):

                frq_vects_expt[ii, aa, bb] = frq_vects_theory[ii, aa, bb]

                # only do fitting if peak size exceeds tolerance, and only fit one of a peak and its compliment
                if not to_use[ii, aa, bb]:
                    continue

                max_frq_shift = np.min([max_frq_shift_pix * dfx, 0.5 * vnorms[aa, bb], 0.5 * min_sep])

                # get experimental frequency
                if (max_frq_shift/dfx) < 1 or use_guess_frqs or np.linalg.norm(frq_vects_expt[ii, aa, bb]) == 0:
                    # if can't get large enough ROI, then use our guess
                    pass
                else:
                    # fit real fourier component in image space
                    # only need wavelength and na to get fmax
                    frq_vects_expt[ii, aa, bb], mask, _ = sim_reconstruction.fit_modulation_frq(
                        img_ft, img_ft, pixel_size_um, fmax_img,
                        frq_guess=frq_vects_theory[ii, aa, bb], max_frq_shift=max_frq_shift,
                        force_start_from_guess=force_start_from_guess)

                    sim_reconstruction.plot_correlation_fit(img_ft, img_ft, frq_vects_expt[ii, aa, bb],
                                                            pixel_size_um, fmax_img,
                                                            frqs_guess=frq_vects_theory[ii, aa, bb], roi_size=3)

                try:
                    # get peak value and phase
                    intensity[ii, aa, bb] = tools.get_peak_value(img_ft, fxs, fys, frq_vects_expt[ii, aa, bb],
                                                                         peak_pixel_size=peak_pix)

                    intensity_unc[ii, aa, bb] = np.sqrt(noise_power) * peak_pix ** 2

                    # handle complimentary point with aa > bb
                    aa_neg = n1_vecs - 1 - aa
                    bb_neg = n2_vecs - 1 - bb
                    intensity[ii, aa_neg, bb_neg] = intensity[ii, aa, bb].conj()
                    intensity_unc[ii, aa_neg, bb_neg] = intensity_unc[ii, aa, bb]

                except:
                    pass

    return intensity, intensity_unc, frq_vects_expt


def get_all_fourier_thry(vas,
                         vbs,
                         nmax,
                         nphases,
                         phase_index,
                         dmd_size):
    """
    Calculate theory intensity/electric field fourier components

    :param vas:
    :param vbs:
    :param nmax:
    :param nphases:
    :param phase_index:
    :param dmd_size:

    :return efield_theory:
    :return ns:
    :return ms:
    :return frq_vects_dmd:
    """
    npatterns = len(vas)

    norders = 2 * nmax + 1
    efield_theory = np.zeros((npatterns, norders, norders), dtype=np.complex) * np.nan
    frq_vects_dmd = np.zeros((npatterns, norders, norders, 2))

    tstart = time.process_time()
    for ii in range(npatterns):
        tnow = time.process_time()
        print("%d/%d, elapsed time = %0.2fs" % (ii + 1, npatterns, tnow - tstart))
        # sys.stdout.write("\033[F")
        # sys.stdout.flush()
        va = vas[ii]
        vb = vbs[ii]

        # recp_vec_a, recp_vec_b = dmd_patterns.get_reciprocal_vects(va, vb)
        unit_cell, xcell, ycell = dmd_patterns.get_sim_unit_cell(va, vb, nphases)

        # get expected values
        efield_theory[ii], ns, ms, frq_vects_dmd[ii] = \
            dmd_patterns.get_efield_fourier_components(unit_cell, xcell, ycell, va, vb, nphases, phase_index,
                                                       dmd_size=dmd_size, nmax=nmax)

        # change normalization from 1 being maximum possible fourier component to 1 being DC component
        efield_theory[ii] = efield_theory[ii] / np.nansum(unit_cell) * np.nansum(unit_cell >= 0)

    return efield_theory, ns, ms, frq_vects_dmd


def get_intensity_fourier_thry(efields,
                               frq_vects_dmd,
                               roi: list[int],
                               affine_xform,
                               wavelength_ex: float,
                               fmax_efield_ex: float,
                               index_of_refraction: float,
                               pixel_size_um: float,
                               dmd_shape: tuple[int],
                               use_blaze_correction: bool = False,
                               use_polarization_correction: bool = False,
                               dmd_params: dict = None):
    """

    :param efields: nimgs x nvecs1 x nvecs2, electric field Fourier components from DMD pattern, with no blaze
     condition corrections.
    :param frq_vects_dmd: frequency vectors in 1/mirrors in DMD space. Size nimgs x nvecs1 x nvecs2 x 2
    :param roi: region of interest within image. [ystart, yend, xstart, xend]
    :param affine_xform: affine transformation connecting image space and DMD space
     (using pixels as coordinates on both ends). This is a 3x3 matrix.
    :param wavelength_ex:
    :param fmax_efield_ex: maximum electric field frequency that can pass throught he imaging system from the
     DMD to sample, in 1/mirrors
    :param index_of_refraction:
    :param pixel_size_um: camera pixel size in um
    :param dmd_shape:
    :param use_blaze_correction: whether or not to use blaze correction
    :param use_polarization_correction: whether or not to correct for polarization effects. Assumes input is
    completely unpolarized. # todo: can add other polarization options
    :param dmd_params: {"wavelength", "gamma", "wx", "wy", "theta_ins": [tx, ty], "theta_outs": [tx, ty]}. These can be
    omitted if use_blaze_correction is False

    :return intensity_theory: theoretical intensity using DMD coordinates. nimgs x nvecs1 x nvecs2
    :return intensity_theory_xformed: theoretical intensity using camera ROI coordinates. Same magnitude but different
    phase versus intensity_theory. nimgs x nvecs1 x nvecs2. This is identical to intensity_theory in magnitude, but
    different in phase
    :return frq_vects_cam: frequency vectors in camera space in 1/pixels. nimgs x nvecs1 x nvecs2 x 2
    :return frq_vects_um: frequency vectors in camera space in 1/microns. nimgs x nvecs1 x nvecs2 x 2
    """

    # define pupil function
    if use_blaze_correction:
        def pupil_fn(fx, fy):
            fx = np.atleast_1d(fx)
            fy = np.atleast_1d(fy)
            tx_out = dmd_params["theta_outs"][0] + dmd_params["wavelength"] * fx / dmd_params["dx"]
            ty_out = dmd_params["theta_outs"][1] + dmd_params["wavelength"] * fy / dmd_params["dy"]

            uvec_in = simulate_dmd.xy2uvector(dmd_params["theta_ins"][0], dmd_params["theta_ins"][1], "in")
            uvec_out = simulate_dmd.xy2uvector(tx_out, ty_out, "out")
            envelope = simulate_dmd.blaze_envelope(dmd_params["wavelength"], dmd_params["gamma"], dmd_params["wx"],
                                               dmd_params["wy"], uvec_in - uvec_out)
            return envelope * (np.sqrt(fx ** 2 + fy ** 2) <= fmax_efield_ex)
    else:
        def pupil_fn(fx, fy):
            return np.sqrt(fx ** 2 + fy ** 2) <= fmax_efield_ex

    # compute pupil
    pupil = pupil_fn(frq_vects_dmd[..., 0], frq_vects_dmd[..., 1])

    # compute frequency vectors in camera space (1/pixels)
    frq_vects_cam = np.zeros(frq_vects_dmd.shape)
    frq_vects_cam[..., 0], frq_vects_cam[..., 1], _ = affine.xform_sinusoid_params_roi(
        frq_vects_dmd[..., 0], frq_vects_dmd[..., 1], 0, dmd_shape, roi, affine_xform,
        input_origin="fft", output_origin="fft")

    # correct frequency vectors in camera space to be in real units (1/um)
    frq_vects_um = frq_vects_cam / pixel_size_um

    # calculate intensities for each image
    intensity_theory = np.zeros(efields.shape, dtype=np.complex) * np.nan
    for ii in range(frq_vects_cam.shape[0]):
        if use_polarization_correction:
            intensity_theory[ii] = get_int_fc_pol(efields[ii] * pupil[ii], frq_vects_um[ii], wavelength_ex, index_of_refraction)
        else:
            intensity_theory[ii] = get_int_fc(efields[ii] * pupil[ii])

    # normalize to DC values
    intensity_theory = intensity_theory / np.max(np.abs(intensity_theory), axis=(1, 2))[:, None, None]

    # compute phase in new coordinates
    _, _, intensity_phases = affine.xform_sinusoid_params_roi(frq_vects_dmd[..., 0], frq_vects_dmd[..., 1],
                                                                      np.angle(intensity_theory),
                                                                      dmd_shape, roi, affine_xform, input_origin="fft", output_origin="fft")
    intensity_theory_xformed = np.abs(intensity_theory) * np.exp(1j * intensity_phases)

    return intensity_theory, intensity_theory_xformed, frq_vects_cam, frq_vects_um

def fit_phase_diff(phase_th,
                   phase_expt,
                   frqs):
    """
    Match theory and experimental phases as best as possible by modifying affine transformation
    :param phase_th:
    :param phase_expt:
    :param frqs:
    :return:
    """
    def phase_diff_fn(phi1, phi2):
        diff = np.array(np.mod(phi1 - phi2, 2*np.pi))
        to_flip = np.abs(diff - 2*np.pi) < np.abs(diff)
        diff[to_flip] = diff[to_flip] - 2*np.pi
        return diff

    def phase_xform_fn(phi, fx, fy, p): return np.mod(phi + 2 * np.pi * fx * p[0] + 2 * np.pi * fy * p[1], 2 * np.pi)

    def min_fn(p): return np.nansum(np.abs(phase_diff_fn(phase_xform_fn(phase_th, frqs[:, 0], frqs[:, 1], p), phase_expt)))

    results = scipy.optimize.minimize(min_fn, [0, 0])

    figh = plt.figure()
    plt.plot(np.linalg.norm(frqs, axis=-1), phase_xform_fn(phase_th, frqs[:, 0], frqs[:, 1], results["x"]), '.')
    plt.plot(np.linalg.norm(frqs, axis=-1), np.mod(phase_th, 2*np.pi), '.')
    plt.plot(np.linalg.norm(frqs, axis=-1), np.mod(phase_expt, 2*np.pi), 'x')
    plt.legend(["th fit", "th", "expt"])

    return figh, results


def plot_pattern(img: np.ndarray,
                 va: list[int],
                 vb: list[int],
                 frq_vects,
                 fmax_img: float,
                 pixel_size_um: float,
                 dmd_size: tuple[int],
                 affine_xform,
                 roi: list[int],
                 nphases: int,
                 phase_index: int,
                 fmax_in=None,
                 peak_int_exp=None,
                 peak_int_exp_unc=None,
                 peak_int_theory=None,
                 otf=None,
                 otf_unc=None,
                 to_use=None,
                 figsize=(20, 10)):
    """
    plot image and affine xformed pattern it corresponds to
    :param img: image ny x nx
    :param va: [vx, vy]
    :param vb: [vx, vy]
    :param frq_vects: vecs1 x nvecs2 x 2. in 1/um
    :param fmax_img: in 1/um
    :param pixel_size_um:
    :param dmd_size: []
    :param affine_xform: affine transformation between DMD space and camera space
    :param roi: [ystart, yend, xstart, xend] region of interest in image
    :param nphases: number of phaseshifts used to generate the DMD pattern. Needed in addition to va/vb to specify pattern
    :param phase_index:  index of phaseshift. Needed in addition to va, vb, nphases to specify pattern

    :return fig_handle:
    """

    if to_use is None:
        to_use = np.ones(peak_int_exp.shape, dtype=np.bool)

    fmags = np.linalg.norm(frq_vects, axis=-1)
    n1max = int(np.round(0.5 * (fmags.shape[0] - 1)))
    n2max = int(np.round(0.5 * (fmags.shape[1] - 1)))

    # generate DMD pattern
    pattern, _ = dmd_patterns.get_sim_pattern(dmd_size, va, vb, nphases, phase_index)

    # crop image
    img_roi = img[roi[0]:roi[1], roi[2]:roi[3]]
    ny, nx = img_roi.shape

    # transform pattern using affine transformation
    xform_roi = affine.xform_shift_center(affine_xform, cimg_new=(roi[2], roi[0]))
    img_coords = np.meshgrid(range(nx), range(ny))
    pattern_xformed = affine.xform_mat(pattern, xform_roi, img_coords, mode="interp")
    # pattern_xformed_ft = fft.fftshift(fft.fft2(fft.ifftshift(pattern_xformed)))

    # get fourier transform of image
    # fxs = tools.get_fft_frqs(nx, pixel_size_um)
    fxs = fft.fftshift(fft.fftfreq(nx, pixel_size_um))
    dfx = fxs[1] - fxs[0]
    # fys = tools.get_fft_frqs(ny, pixel_size_um)
    fys = fft.fftshift(fft.fftfreq(ny, pixel_size_um))
    dfy = fys[1] - fys[0]

    extent = [fxs[0] - 0.5 * dfx, fxs[-1] + 0.5 * dfx, fys[-1] + 0.5 * dfy, fys[0] - 0.5 * dfy]


    window = scipy.signal.windows.hann(nx)[None, :] * scipy.signal.windows.hann(ny)[:, None]
    img_ft = fft.fftshift(fft.fft2(fft.ifftshift(img_roi * window)))

    # plot results
    figh = plt.figure(figsize=figsize)
    grid = plt.GridSpec(2, 6)

    period = dmd_patterns.get_sim_period(va, vb)
    angle = dmd_patterns.get_sim_angle(va, vb)
    frq_main_um = frq_vects[n1max, n2max + 1]

    plt.suptitle("DMD period=%0.3f mirrors, angle=%0.2fdeg\n"
                 "Camera period=%0.1fnm = 1/%0.3f um, angle=%0.2fdeg\n"
                 "va=(%d, %d); vb=(%d, %d)" % (period, angle * 180/np.pi, 1 / np.linalg.norm(frq_main_um) * 1e3,
                                               np.linalg.norm(frq_main_um),
                                               np.mod(np.angle(frq_main_um[0] + 1j * frq_main_um[1]), 2*np.pi) * 180/np.pi,
                                               va[0], va[1], vb[0], vb[1]))

    plt.subplot(grid[0, 0:2])
    plt.imshow(img_roi)
    plt.title('image')

    plt.subplot(grid[0, 2:4])
    plt.imshow(pattern_xformed)
    plt.title('pattern after affine xform')

    ax = plt.subplot(grid[0, 4:6])
    plt.title('image FFT')

    plt.imshow(np.abs(img_ft) ** 2, norm=PowerNorm(gamma=0.1), extent=extent)

    # to_plot = np.logical_not(np.isnan(intensity_exp_norm))
    nmax = int(np.round((frq_vects.shape[1] - 1) * 0.5))
    plt.scatter(frq_vects[to_use, 0].ravel(), frq_vects[to_use, 1].ravel(), facecolor='none', edgecolor='r')
    plt.scatter(-frq_vects[to_use, 0].ravel(), -frq_vects[to_use, 1].ravel(), facecolor='none', edgecolor='m')
    # plt.scatter(frq_vects[nmax, nmax + 1, 0], frq_vects[nmax, nmax + 1, 1], facecolor="none", edgecolor='k')
    # plt.scatter(frq_vects[nmax, nmax - 1, 0], frq_vects[nmax, nmax - 1, 1], facecolor="none", edgecolor='k')
    # plt.scatter(frq_vects[nmax, nmax + 2, 0], frq_vects[nmax, nmax + 2, 1], facecolor="none", edgecolor='k')
    # plt.scatter(frq_vects[nmax, nmax - 2, 0], frq_vects[nmax, nmax - 2, 1], facecolor="none", edgecolor='k')

    circ = matplotlib.patches.Circle((0, 0), radius=fmax_img, color='k', fill=0, ls='--')
    ax.add_artist(circ)

    if fmax_in is not None:
        circ2 = matplotlib.patches.Circle((0, 0), radius=(fmax_in/2), color='r', fill=0, ls='--')
        ax.add_artist(circ2)


    plt.xlim([-fmax_img, fmax_img])
    plt.ylim([fmax_img, -fmax_img])

    ax = plt.subplot(grid[1, :2])
    plt.title("peaks amp expt/theory")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("Intensity")

    plt.plot([fmax_img, fmax_img], [0, 1], 'k')

    phs = []
    legend_entries = []

    if peak_int_theory is not None:
        ph, = ax.plot(fmags[to_use], np.abs(peak_int_theory[to_use]).ravel() / np.nanmax(np.abs(peak_int_theory[to_use])), '.')
        phs.append(ph)
        legend_entries.append("theory")

    if peak_int_exp is not None:
        if peak_int_exp_unc is None:
            peak_int_exp_unc = np.zeros(peak_int_exp.shape)

        ph = ax.errorbar(fmags[to_use], np.abs(peak_int_exp[to_use]).ravel() / np.nanmax(np.abs(peak_int_exp)),
                         yerr=peak_int_exp_unc[to_use].ravel() / np.nanmax(np.abs(peak_int_exp)), fmt='x')
        phs.append(ph)
        legend_entries.append("experiment")


    ax.set_ylim([1e-4, 1.2])
    ax.set_xlim([-0.1 * fmax_img, 1.1 * fmax_img])
    ax.set_yscale('log')

    plt.legend(phs, legend_entries)


    # plot phase
    ax = plt.subplot(grid[1, 2:4])
    plt.title("peaks phase expt/theory")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("phase")

    plt.plot([fmax_img, fmax_img], [-np.pi, np.pi], 'k')

    phs = []
    legend_entries = []

    if peak_int_theory is not None:
        ph, = ax.plot(fmags[to_use], np.angle(peak_int_theory[to_use]).ravel(), '.')
        phs.append(ph)
        legend_entries.append("theory")

    if peak_int_exp is not None:
        ph, = ax.plot(fmags[to_use], np.angle(peak_int_exp[to_use]).ravel(), 'x')
        phs.append(ph)
        legend_entries.append("experiment")

    ax.set_xlim([-0.1 * fmax_img, 1.1 * fmax_img])

    plt.legend(phs, legend_entries)

    # plot otf
    ax = plt.subplot(grid[1, 4:])
    plt.title("otf")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("otf")

    ax.plot([0, fmax_img], [0, 0], 'k')
    ax.plot([fmax_img, fmax_img], [0, 1], 'k')
    if otf is not None:
        if otf_unc is None:
            otf_unc = np.zeros(otf.shape)
        ax.errorbar(fmags[to_use], np.abs(otf[to_use]).ravel(), yerr=otf_unc[to_use].ravel(), fmt='.')

    ax.set_ylim([-0.05, 1.2])
    ax.set_xlim([-0.1 * fmax_img, 1.1 * fmax_img])

    return figh

def plot_otf(frq_vects,
             fmax_img: float,
             otf,
             otf_unc=None,
             to_use=None,
             wf_corrected=None,
             figsize: tuple[float] = (20, 10)):
    """
    Plot complete OTF
    :param frq_vects:
    :param fmax_img:
    :param otf:
    :param otf_unc:
    :param to_use:
    :param wf_corrected:
    :param figsize:
    :return:
    """
    if otf_unc is None:
        otf_unc = np.zeros(otf.shape)

    if to_use is None:
        to_use = np.ones(otf.shape, dtype=np.int)

    nmax1 = int(np.round(0.5 * (otf.shape[1] - 1)))
    nmax2 = int(np.round(0.5 * (otf.shape[2] - 1)))


    fmag = np.linalg.norm(frq_vects, axis=-1)

    fmag_interp = np.linspace(0, fmax_img, 1000)
    # only care about fmax value, so create na/wavelength that give us this
    na = 1
    wavelength = 2 * na / fmax_img
    otf_ideal = fit_psf.circ_aperture_otf(fmag_interp, 0, na, wavelength)


    figh = plt.figure(figsize=figsize)
    grid = plt.GridSpec(2, 6)

    # 1D otf
    ax = plt.subplot(grid[0, :2])
    ylim = [-0.05, 1.2]
    plt.title("otf mag")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("otf")

    ph_ideal, = plt.plot(fmag_interp, otf_ideal, 'k')
    plt.errorbar(fmag[to_use], np.abs(otf[to_use]), yerr=otf_unc[to_use], color="b", fmt='.')

    colors = ["g", "m", "r", "y", "c"]
    phs = [ph_ideal]
    labels = ["OTF ideal, fmax=%0.2f (1/um)" % fmax_img] + list(range(1, 6))
    # plot main series peaks
    for jj in range(1, 6):
        ph = plt.errorbar(fmag[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]],
                     np.abs(otf[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]]),
                     yerr=otf_unc[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]],
                     color=colors[jj - 1], fmt=".")
        phs.append(ph)

        plt.errorbar(fmag[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]],
                     np.abs(otf[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]]),
                     yerr=otf_unc[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]],
                     color=colors[jj - 1], fmt=".")

    plt.legend(phs, labels)

    xlim = ax.get_xlim()
    plt.plot(xlim, [0, 0], 'k')
    plt.plot([fmax_img, fmax_img], ylim, 'k')
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    # 1D log scale
    ax = plt.subplot(grid[1, :2])
    plt.title("otf mag (log scale)")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("otf")

    plt.plot(fmag_interp, otf_ideal, 'k')
    plt.errorbar(fmag[to_use], np.abs(otf[to_use]), yerr=otf_unc[to_use], fmt='.')

    # plot main series peaks
    for jj in range(1, 6):
        plt.errorbar(fmag[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]],
                     np.abs(otf[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]]),
                     yerr=otf_unc[:, nmax1, nmax2 + jj][to_use[:, nmax1, nmax2 + jj]],
                     color=colors[jj - 1], fmt=".")

        plt.errorbar(fmag[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]],
                     np.abs(otf[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]]),
                     yerr=otf_unc[:, nmax1, nmax2 - jj][to_use[:, nmax1, nmax2 - jj]],
                     color=colors[jj - 1], fmt=".")

    xlim = ax.get_xlim()
    ax.plot([fmax_img, fmax_img], ylim, 'k')
    ax.set_xlim(xlim)
    ax.set_ylim([1e-4, 1.2])

    ax.set_yscale('log')

    # show widefield corrected/not peaks
    ax = plt.subplot(grid[1, 4:])
    ylim = [-0.05, 1.2]
    plt.title("otf mag, widefield corrected/not")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("otf")

    plt.plot(fmag_interp, otf_ideal, 'k')
    phu = plt.errorbar(fmag[to_use], np.abs(otf[to_use]), yerr=otf_unc[to_use], color="b", fmt='.')

    corrected = np.logical_and(wf_corrected, to_use)
    phc = plt.errorbar(fmag[corrected], np.abs(otf[corrected]), yerr=otf_unc[corrected], color="r", fmt=".")

    xlim = ax.get_xlim()
    plt.plot(xlim, [0, 0], 'k')
    plt.plot([fmax_img, fmax_img], ylim, 'k')
    ax.set_xlim(xlim)
    ax.set_ylim(ylim)

    plt.legend([phu, phc], ["uncorrected peaks", "corrected"])

    # 2D otf
    ax = plt.subplot(grid[0, 2:4])
    plt.title("2D otf (log scale)")
    plt.xlabel("fx (1/um)")
    plt.ylabel("fy (1/um)")
    clims = [1e-3, 1]

    frqs_pos = np.array(frq_vects, copy=True)
    y_is_neg = frq_vects[..., 1] < 0
    frqs_pos[y_is_neg] = -frqs_pos[y_is_neg]

    plt.plot([-fmax_img, fmax_img], [0, 0], 'k')
    plt.scatter(frqs_pos[to_use, 0].ravel(), frqs_pos[to_use, 1].ravel(),
                c=np.log10(np.abs(otf[to_use]).ravel()),
                norm=matplotlib.colors.Normalize(vmin=np.log10(clims[0]), vmax=np.log10(clims[1])))
    cb = plt.colorbar()
    plt.clim(np.log10(clims))

    circ = matplotlib.patches.Circle((0, 0), radius=fmax_img, color='k', fill=0, ls='-')
    ax.add_artist(circ)
    ax.set_xlim([-fmax_img, fmax_img])
    ax.set_ylim([-0.05 * fmax_img, fmax_img])

    # plot phase
    ax = plt.subplot(grid[1, 2:4])
    plt.title("phase")
    plt.xlabel("Frequency (1/um)")
    plt.ylabel("phase")

    ax.plot(fmag[to_use], np.angle(otf[to_use]).ravel(), '.')

    ylims = [-np.pi - 0.1, np.pi + 0.1]
    ax.set_ylim(ylims)

    # plot 2D phase
    ax = plt.subplot(grid[0, 4:])
    plt.title("2D otf phase")
    plt.xlabel("fx (1/um)")
    plt.ylabel("fy (1/um)")
    clims_phase = [-np.pi - 0.1, np.pi + 0.1]

    plt.plot([-fmax_img, fmax_img], [0, 0], 'k')
    plt.scatter(frqs_pos[to_use, 0].ravel(), frqs_pos[to_use, 1].ravel(),
                c=np.angle(otf[to_use]),
                norm=matplotlib.colors.Normalize(vmin=clims_phase[0], vmax=clims_phase[1]))
    cb = plt.colorbar()
    plt.clim(clims_phase)

    circ = matplotlib.patches.Circle((0, 0), radius=fmax_img, color='k', fill=0, ls='-')
    ax.add_artist(circ)
    ax.set_xlim([-fmax_img, fmax_img])
    ax.set_ylim([-0.05 * fmax_img, fmax_img])

    return figh
