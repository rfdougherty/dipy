from __future__ import division, print_function, absolute_import

from multiprocessing import cpu_count, Pool
from itertools import repeat
from os import path
from warnings import warn

from ..utils.six.moves import xrange

from nibabel.tmpdirs import InTemporaryDirectory

import numpy as np
import scipy.optimize as opt

from .recspeed import local_maxima, remove_similar_vertices, search_descending
from dipy.core.sphere import HemiSphere, Sphere
from dipy.data import get_sphere
from dipy.core.ndindex import ndindex
from dipy.reconst.shm import sh_to_sf_matrix

default_sphere = HemiSphere.from_sphere(get_sphere('symmetric724'))


def peak_directions_nl(sphere_eval, relative_peak_threshold=.25,
                       min_separation_angle=45, sphere=default_sphere,
                       xtol=1e-7):
    """Non Linear Direction Finder

    Parameters
    ----------
    sphere_eval : callable
        A function which can be evaluated on a sphere.
    relative_peak_threshold : float
        Only return peaks greater than ``relative_peak_threshold * m`` where m
        is the largest peak.
    min_separation_angle : float in [0, 90]
        The minimum distance between directions. If two peaks are too close
        only the larger of the two is returned.
    sphere : Sphere
        A discrete Sphere. The points on the sphere will be used for initial
        estimate of maximums.
    xtol : float
        Relative tolerance for optimization.

    Returns
    -------
    directions : array (N, 3)
        Points on the sphere corresponding to N local maxima on the sphere.
    values : array (N,)
        Value of sphere_eval at each point on directions.

    """
    # Find discrete peaks for use as seeds in non-linear search
    discrete_values = sphere_eval(sphere)
    values, indices = local_maxima(discrete_values, sphere.edges)

    seeds = np.column_stack([sphere.theta[indices], sphere.phi[indices]])

    # Helper function
    def _helper(x):
        sphere = Sphere(theta=x[0], phi=x[1])
        return -sphere_eval(sphere)

    # Non-linear search
    num_seeds = len(seeds)
    theta = np.empty(num_seeds)
    phi = np.empty(num_seeds)
    for i in xrange(num_seeds):
        peak = opt.fmin(_helper, seeds[i], xtol=xtol, disp=False)
        theta[i], phi[i] = peak

    # Evaluate on new-found peaks
    small_sphere = Sphere(theta=theta, phi=phi)
    values = sphere_eval(small_sphere)

    # Sort in descending order
    order = values.argsort()[::-1]
    values = values[order]
    directions = small_sphere.vertices[order]

    # Remove directions that are too small
    n = search_descending(values, relative_peak_threshold)
    directions = directions[:n]

    # Remove peaks too close to each-other
    directions, idx = remove_similar_vertices(directions, min_separation_angle,
                                              return_index=True)
    values = values[idx]
    return directions, values


def peak_directions(odf, sphere, relative_peak_threshold=.25,
                    min_separation_angle=45):
    """Get the directions of odf peaks

    Parameters
    ----------
    odf : 1d ndarray
        The odf function evaluated on the vertices of `sphere`
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    relative_peak_threshold : float
        Only return peaks greater than ``relative_peak_threshold * m`` where m
        is the largest peak.
    min_separation_angle : float in [0, 90] The minimum distance between
        directions. If two peaks are too close only the larger of the two is
        returned.

    Returns
    -------
    directions : (N, 3) ndarray
        N vertices for sphere, one for each peak
    values : (N,) ndarray
        peak values
    indices : (N,) ndarray
        peak indices of the directions on the sphere

    """
    odf = np.ascontiguousarray(odf)
    values, indices = local_maxima(odf, sphere.edges)
    # If there is only one peak return
    if len(indices) == 1:
        return sphere.vertices[indices], values, indices

    n = search_descending(values, relative_peak_threshold)
    indices = indices[:n]
    directions = sphere.vertices[indices]
    directions, uniq = remove_similar_vertices(directions,
                                               min_separation_angle,
                                               return_index=True)
    values = values[uniq]
    indices = indices[uniq]
    return directions, values, indices


class PeaksAndMetrics(object):
    pass


def _peaks_from_model_parallel(model, data, sphere, relative_peak_threshold,
                               min_separation_angle, mask, return_odf,
                               return_sh, gfa_thr, normalize_peaks,
                               sh_order, sh_basis_type, npeaks, B, invB, nbr_processes):

    if nbr_processes is None:
        try:
            nbr_processes = cpu_count()
        except NotImplementedError:
            warn("Cannot determine number of cpus. \
                 returns peaks_from_model(..., paralle=False).")
            return peaks_from_model(model, data, sphere,
                                    relative_peak_threshold,
                                    min_separation_angle, mask, return_odf,
                                    return_sh, gfa_thr, normalize_peaks,
                                    sh_order, sh_basis_type, npeaks,
                                    parallel=False)

    shape = list(data.shape)
    data = np.reshape(data, (-1, shape[-1]))
    n = data.shape[0]
    nbr_chunks = nbr_processes ** 2
    chunk_size = int(np.ceil(n / nbr_chunks))
    indices = zip(np.arange(0, n, chunk_size),
                  np.arange(0, n, chunk_size) + chunk_size)

    with InTemporaryDirectory() as tmpdir:

        data_file_name = path.join(tmpdir, 'data.npy')
        np.save(data_file_name, data)
        if mask is not None:
            mask = mask.flatten()
            mask_file_name = path.join(tmpdir, 'mask.npy')
            np.save(mask_file_name, mask)
        else:
            mask_file_name = None

        pool = Pool(nbr_processes)

        pam_res = pool.map(_peaks_from_model_parallel_sub,
                           zip(repeat((data_file_name, mask_file_name)),
                               indices,
                               repeat(model),
                               repeat(sphere),
                               repeat(relative_peak_threshold),
                               repeat(min_separation_angle),
                               repeat(return_odf),
                               repeat(return_sh),
                               repeat(gfa_thr),
                               repeat(normalize_peaks),
                               repeat(sh_order),
                               repeat(sh_basis_type),
                               repeat(npeaks),
                               repeat(B),
                               repeat(invB)))
        pool.close()

        pam = PeaksAndMetrics()

        # use memmap to reduce the memory usage
        pam.gfa = np.memmap(path.join(tmpdir, 'gfa.npy'),
                            dtype=pam_res[0].gfa.dtype,
                            mode='w+',
                            shape=(data.shape[0]))

        pam.peak_dirs = np.memmap(path.join(tmpdir, 'peak_dirs.npy'),
                                  dtype=pam_res[0].peak_dirs.dtype,
                                  mode='w+',
                                  shape=(data.shape[0], npeaks, 3))
        pam.peak_values = np.memmap(path.join(tmpdir, 'peak_values.npy'),
                                    dtype=pam_res[0].peak_values.dtype,
                                    mode='w+',
                                    shape=(data.shape[0], npeaks))
        pam.peak_indices = np.memmap(path.join(tmpdir, 'peak_indices.npy'),
                                     dtype=pam_res[0].peak_indices.dtype,
                                     mode='w+',
                                     shape=(data.shape[0], npeaks))
        pam.qa = np.memmap(path.join(tmpdir, 'qa.npy'),
                           dtype=pam_res[0].qa.dtype,
                           mode='w+',
                           shape=(data.shape[0], npeaks))
        if return_sh:
            nbr_shm_coeff = (sh_order + 2) * (sh_order + 1) / 2
            pam.shm_coeff = np.memmap(path.join(tmpdir, 'shm.npy'),
                                      dtype=pam_res[0].shm_coeff.dtype,
                                      mode='w+',
                                      shape=(data.shape[0], nbr_shm_coeff))
            pam.B = pam_res[0].B
        else:
            pam.shm_coeff = None
            pam.invB = None
        if return_odf:
            pam.odf = np.memmap(path.join(tmpdir, 'odf.npy'),
                                dtype=pam_res[0].odf.dtype,
                                mode='w+',
                                shape=(data.shape[0], len(sphere.vertices)))
        else:
            pam.odf = None

        # copy subprocesses pam to a single pam (memmaps)
        for i, (start_pos, end_pos) in enumerate(indices):
            pam.gfa[start_pos: end_pos] = pam_res[i].gfa[:]
            pam.peak_dirs[start_pos: end_pos] = pam_res[i].peak_dirs[:]
            pam.peak_values[start_pos: end_pos] = pam_res[i].peak_values[:]
            pam.peak_indices[start_pos: end_pos] = pam_res[i].peak_indices[:]
            pam.qa[start_pos: end_pos] = pam_res[i].qa[:]
            if return_sh:
                pam.shm_coeff[start_pos: end_pos] = pam_res[i].shm_coeff[:]
            if return_odf:
                pam.odf[start_pos: end_pos] = pam_res[i].odf[:]

        pam_res = None

        # load memmaps to arrays and reshape the metric
        shape[-1] = -1
        pam.gfa = np.reshape(np.array(pam.gfa), shape[:-1])
        pam.peak_dirs = np.reshape(np.array(pam.peak_dirs), shape[:] + [3])
        pam.peak_values = np.reshape(np.array(pam.peak_values), shape[:])
        pam.peak_indices = np.reshape(np.array(pam.peak_indices), shape[:])
        pam.qa = np.reshape(np.array(pam.qa), shape[:])
        if return_sh:
            pam.shm_coeff = np.reshape(np.array(pam.shm_coeff), shape[:])
        if return_odf:
            pam.odf = np.reshape(np.array(pam.odf), shape[:])

        # Make sure all worker processes have exited before leaving context
        # manager in order to prevent temporary file deletion errors in windows
        pool.join()

    return pam


def _peaks_from_model_parallel_sub(args):
    (data_file_name, mask_file_name) = args[0]
    (start_pos, end_pos) = args[1]
    model = args[2]
    sphere = args[3]
    relative_peak_threshold = args[4]
    min_separation_angle = args[5]
    return_odf = args[6]
    return_sh = args[7]
    gfa_thr = args[8]
    normalize_peaks = args[9]
    sh_order = args[10]
    sh_basis_type = args[11]
    npeaks = args[12]
    B = args[13]
    invB = args[14]

    data = np.load(data_file_name, mmap_mode='r')[start_pos:end_pos]
    if mask_file_name is not None:
        mask = np.load(mask_file_name, mmap_mode='r')[start_pos:end_pos]
    else:
        mask = None

    return peaks_from_model(model, data, sphere, relative_peak_threshold,
                            min_separation_angle, mask, return_odf,
                            return_sh, gfa_thr, normalize_peaks,
                            sh_order, sh_basis_type, npeaks, B, invB,
                            parallel=False, nbr_processes=None)


def peaks_from_model(model, data, sphere, relative_peak_threshold,
                     min_separation_angle, mask=None, return_odf=False,
                     return_sh=True, gfa_thr=0, normalize_peaks=False,
                     sh_order=8, sh_basis_type=None, npeaks=5, B=None, invB=None,
                     parallel=False, nbr_processes=None):
    """Fits the model to data and computes peaks and metrics

    Parameters
    ----------
    model : a model instance
        `model` will be used to fit the data.
    sphere : Sphere
        The Sphere providing discrete directions for evaluation.
    relative_peak_threshold : float
        Only return peaks greater than ``relative_peak_threshold * m`` where m
        is the largest peak.
    min_separation_angle : float in [0, 90] The minimum distance between
        directions. If two peaks are too close only the larger of the two is
        returned.
    mask : array, optional
        If `mask` is provided, voxels that are False in `mask` are skipped and
        no peaks are returned.
    return_odf : bool
        If True, the odfs are returned.
    return_sh : bool
        If True, the odf as spherical harmonics coefficients is returned
    gfa_thr : float
        Voxels with gfa less than `gfa_thr` are skipped, no peaks are returned.
    normalize_peaks : bool
        If true, all peak values are calculated relative to `max(odf)`.
    sh_order : int, optional
        Maximum SH order in the SH fit.  For `sh_order`, there will be
        ``(sh_order + 1) * (sh_order + 2) / 2`` SH coefficients (default 8).
    sh_basis_type : {None, 'mrtrix', 'fibernav'}
        ``None`` for the default dipy basis which is the fibernav basis,
        ``mrtrix`` for the MRtrix basis, and
        ``fibernav`` for the FiberNavigator basis
    sh_smooth : float, optional
        Lambda-regularization in the SH fit (default 0.0).
    npeaks : int
        Maximum number of peaks found (default 5 peaks).
    B : ndarray, optional
        Matrix that transforms spherical harmonics to spherical function
        ``sf = np.dot(sh, B)``.
    invB : ndarray, optional
        Inverse of B.
    parallel: bool
        If True, use multiprocessing to compute peaks and metric
        (default False).
    nbr_processes: int
        If `parallel == True`, the number of subprocesses to use
        (default multiprocessing.cpu_count()).

    Returns
    -------
    pam : PeaksAndMetrics
        An object with ``gfa``, ``peak_directions``, ``peak_values``,
        ``peak_indices``, ``odf``, ``shm_coeffs`` as attributes
    """

    if return_sh and (B is None or invB is None):
        B, invB = sh_to_sf_matrix(
            sphere, sh_order, sh_basis_type, return_inv=True)

    if parallel:
        # It is mandatory to provides B and invB to the parallel function.
        # Otherwise, a call to np.linalg.pinv is made in a subprocess and
        # makes it timeout on some system.
        # see https://github.com/nipy/dipy/issues/253 for details
        return _peaks_from_model_parallel(model,
                                          data, sphere,
                                          relative_peak_threshold,
                                          min_separation_angle,
                                          mask, return_odf,
                                          return_sh,
                                          gfa_thr,
                                          normalize_peaks,
                                          sh_order,
                                          sh_basis_type,
                                          npeaks,
                                          B,
                                          invB,
                                          nbr_processes)

    shape = data.shape[:-1]
    if mask is None:
        mask = np.ones(shape, dtype='bool')
    else:
        if mask.shape != shape:
            raise ValueError("Mask is not the same shape as data.")

    gfa_array = np.zeros(shape)
    qa_array = np.zeros((shape + (npeaks,)))

    peak_dirs = np.zeros((shape + (npeaks, 3)))
    peak_values = np.zeros((shape + (npeaks,)))
    peak_indices = np.zeros((shape + (npeaks,)), dtype='int')
    peak_indices.fill(-1)

    if return_sh:
        n_shm_coeff = (sh_order + 2) * (sh_order + 1) / 2
        shm_coeff = np.zeros((shape + (n_shm_coeff,)))

    if return_odf:
        odf_array = np.zeros((shape + (len(sphere.vertices),)))

    global_max = -np.inf
    for idx in ndindex(shape):
        if not mask[idx]:
            continue

        odf = model.fit(data[idx]).odf(sphere)

        if return_sh:
            shm_coeff[idx] = np.dot(odf, invB)

        if return_odf:
            odf_array[idx] = odf

        gfa_array[idx] = gfa(odf)
        if gfa_array[idx] < gfa_thr:
            global_max = max(global_max, odf.max())
            continue

        # Get peaks of odf
        direction, pk, ind = peak_directions(
            odf, sphere, relative_peak_threshold,
            min_separation_angle)

        # Calculate peak metrics
        global_max = max(global_max, pk[0])
        n = min(npeaks, len(pk))
        qa_array[idx][:n] = pk[:n] - odf.min()

        peak_dirs[idx][:n] = direction[:n]
        peak_indices[idx][:n] = ind[:n]
        peak_values[idx][:n] = pk[:n]

        if normalize_peaks:
            peak_values[idx][:n] /= pk[0]
            peak_dirs[idx] *= peak_values[idx][:, None]

    qa_array /= global_max

    pam = PeaksAndMetrics()
    pam.peak_dirs = peak_dirs
    pam.peak_values = peak_values
    pam.peak_indices = peak_indices
    pam.gfa = gfa_array
    pam.qa = qa_array

    if return_sh:
        pam.shm_coeff = shm_coeff
        pam.B = B
    else:
        pam.shm_coeff = None
        pam.B = None

    if return_odf:
        pam.odf = odf_array
    else:
        pam.odf = None

    return pam


def gfa(samples):
    """The general fractional anisotropy of a function evaluated
    on the unit sphere"""
    diff = samples - samples.mean(-1)[..., None]
    n = samples.shape[-1]
    numer = n * (diff * diff).sum(-1)
    denom = (n - 1) * (samples * samples).sum(-1)
    return np.sqrt(numer / denom)


def minmax_normalize(samples, out=None):
    """Min-max normalization of a function evaluated on the unit sphere

    Normalizes samples to ``(samples - min(samples)) / (max(samples) -
    min(samples))`` for each unit sphere.

    Parameters
    ----------
    samples : ndarray (..., N)
        N samples on a unit sphere for each point, stored along the last axis
        of the array.
    out : ndrray (..., N), optional
        An array to store the normalized samples.

    Returns
    -------
    out : ndarray, (..., N)
        Normalized samples.

    """
    if out is None:
        dtype = np.common_type(np.empty(0, 'float32'), samples)
        out = np.array(samples, dtype=dtype, copy=True)
    else:
        out[:] = samples

    sample_mins = np.min(samples, -1)[..., None]
    sample_maxes = np.max(samples, -1)[..., None]
    out -= sample_mins
    out /= (sample_maxes - sample_mins)
    return out


def reshape_peaks_for_visualization(peaks):
    """Reshape peaks for visualization.

    Reshape and convert to float32 a set of peaks for visualisation with mrtrix
    or the fibernavigator.

    Parameters:
    -----------
    peaks: nd array (..., N, 3) or PeaksAndMetrics object
        The peaks to be reshaped and converted to float32.

    Returns:
    --------
    peaks : nd array (..., 3*N)
    """

    if isinstance(peaks, PeaksAndMetrics):
        peaks = peaks.peak_dirs

    return peaks.reshape(np.append(peaks.shape[:-2], -1)).astype('float32')
