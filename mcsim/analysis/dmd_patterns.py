"""
Generate SIM patterns using lattice periodicity vectors Va and Vb, and duplicating roi_size single unit cell.
See the supplemental material of doi: 10.1038/nmeth.1734 for more discussion of similar approaches.

Note: we interpret the pattern params(x, y) = M[i_y, i_x], where M is the matrix representing the pattern. matplotlib
will display the matrix with i_y = 1 on top, so the pattern we really want is the matrix flipped along the first
dimension.
"""

import time
import json
from pathlib import Path
import copy
import numpy as np
from PIL import Image
from scipy import fft
import scipy.signal
import tifffile
import matplotlib.pyplot as plt
from matplotlib.colors import PowerNorm
from matplotlib.patches import Circle
#
import mcsim.analysis.analysis_tools as tools
from mcsim.analysis import simulate_dmd
from localize_psf import affine


def get_sim_pattern(dmd_size: list[int],
                    vec_a,
                    vec_b,
                    nphases: int,
                    phase_index: int):
    """
    Convenience function for generating SIM patterns from the tile_patterns() function.

    :param dmd_size: [nx, ny]
    :param list or np.array vec_a: [dxa, dya]
    :param list or np.array vec_b: [dxb, dyb]
    :param nphases: number of phase shifts required. This effects the filling of the pattern
    :param phase_index: integer in range(nphases)

    :return pattern, cell: 'pattern' is an array giving the desired pattern and 'cell' is an array giving
    a single unit cell of the pattern
    """

    # ensure both vec_b components are divisible by nphases
    if not (vec_b[0] / nphases).is_integer() or not (vec_b[1] / nphases).is_integer():
        raise ValueError("At least one component of vec_b was not divisible by nphases")

    cell, x, y = get_sim_unit_cell(vec_a, vec_b, nphases)

    vec_b_sub = np.array(vec_b) / nphases
    start_coord = vec_b_sub * phase_index
    pattern = tile_pattern(dmd_size, vec_a, vec_b, start_coord, cell, x, y)
    return pattern, cell


# tool for manipulating unit cells
def tile_pattern(dmd_size: list[int],
                 vec_a,
                 vec_b,
                 start_coord: list[int],
                 cell,
                 x_cell,
                 y_cell,
                 do_cell_reduction: bool = True):
    """
    Generate SIM patterns using lattice periodicity vectors vec_a = [dxa, dya] and vec_b = [dxb, 0],
    and duplicating roi_size single unit cell. See the supplemental material of
    doi: 10.1038/nmeth.1734 for more information.

    # todo: much slower than the old function because looping and doing pixel assignment instead of concatenating

    Note: we interpret the pattern
    params(x, y) = M[i_y, i_x], where M is the matrix representing the pattern. Matlab will display the matrix
    with i_y = 0 on top, so the pattern we really want is the matrix flipped along the first dimension.

    :param dmd_size: [nx, ny]
    :param list or np.array vec_a: [dxa, dya]
    :param list or np.array vec_b: [dxb, dyb]
    :param start_coord: [x, y]. Coordinate to position the start of a unit cell on the DMD.
    This adjusts the phase of the resulting pattern. These coordinates are relative to the image corner
    :param np.array cell:
    :param np.array x_cell:
    :param np.array y_cell:
    :param do_cell_reduction: whether or not to call get_minimal_cell() before tiling

    :return pattern: np.array
    """
    vec_a = np.array(vec_a, copy=True)
    vec_b = np.array(vec_b, copy=True)
    start_coord = np.array(start_coord, copy=True)
    nx, ny = dmd_size

    if do_cell_reduction:
        # this will typically make the vectors shorter and more orthogonal, so tiling is easier
        cell, x_cell, y_cell, vec_a, vec_b = get_minimal_cell(cell, x_cell, y_cell, vec_a, vec_b)

    pattern = np.zeros((ny, nx)) * np.nan

    dy, dx = cell.shape

    # find maximum integer multiples of the periodicity vectors that we need
    # n * vec_a + m * vec_b + start_coord = corners
    # [[dxa, dxb], [dya, dyb]] * [[n], [m]] = [[cx], [cy]] - [[sx], [sy]]
    sx, sy = start_coord
    mat = np.linalg.inv(np.array([[vec_a[0], vec_b[0]], [vec_a[1], vec_b[1]]]))
    n1, m1 = mat.dot(np.array([[pattern.shape[1] - sx], [pattern.shape[0] - sy]], dtype=float))
    n2, m2 = mat.dot(np.array([[0. - sx], [pattern.shape[0] - sy]], dtype=float))
    n3, m3 = mat.dot(np.array([[pattern.shape[1] - sx], [0. - sy]], dtype=float))
    n4, m4 = mat.dot(np.array([[0. - sx], [0. - sy]], dtype=float))

    na_min = int(np.floor(np.min([n1, n2, n3, n4])))
    na_max = int(np.ceil(np.max([n1, n2, n3, n4])))
    nb_min = int(np.floor(np.min([m1, m2, m3, m4])))
    nb_max = int(np.ceil(np.max([m1, m2, m3, m4])))

    niterations = (na_max - na_min) * (nb_max - nb_min)
    if niterations > 1e3:
        # if number of iterations is large, reduce number of tilings required by doubling unit cell
        # todo: could probably find the optimal number of doublings/tilings. This is important to get this to be fast
        # todo:  in a general case
        # todo: right now pretty fast for 'reasonable' patterns, but still seems to be slow for some sets of patterns.
        na_max_doublings = np.floor(np.log2((na_max - na_min)))
        na_doublings = np.max([int(np.round(na_max_doublings / 2)), 1])
        nb_max_doublings = np.floor(np.log2((nb_max - nb_min)))
        nb_doublings = np.max([int(np.round(nb_max_doublings / 2)), 1])
        large_pattern, xp, yp = double_cell(cell, x_cell, y_cell, vec_a, vec_b, na=na_doublings, nb=nb_doublings)

        # finish by tiling
        pattern = tile_pattern(dmd_size, 2 ** na_doublings * vec_a, 2 ** nb_doublings * vec_b,
                               start_coord, large_pattern, xp, yp, do_cell_reduction=False)
    else:
        # for smaller iteration number, tile directly
        for n in range(na_min, na_max + 1):
            for m in range(nb_min, nb_max + 1):
                # account for act the origin of the cell may not be at the lower left corner.
                # (0, 0) position of the cell should be at vec_a * n + vec_b * m + start_coord
                xzero, yzero = vec_a * n + vec_b * m + start_coord
                xstart = int(xzero + np.min(x_cell))
                ystart = int(yzero + np.min(y_cell))
                xend = xstart + int(dx)
                yend = ystart + int(dy)

                if xend < 0 or yend < 0 or xstart > pattern.shape[1] or ystart > pattern.shape[0]:
                    continue

                if xstart < 0:
                    xstart_cell = -xstart
                    xstart = 0
                else:
                    xstart_cell = 0

                if xend > pattern.shape[1]:
                    xend = pattern.shape[1]
                xend_cell = xstart_cell + (xend - xstart)

                if ystart < 0:
                    ystart_cell = -ystart
                    ystart = 0
                else:
                    ystart_cell = 0

                if yend > pattern.shape[0]:
                    yend = pattern.shape[0]
                yend_cell = ystart_cell + (yend - ystart)

                pattern[ystart:yend, xstart:xend] = np.nansum(
                    np.concatenate((pattern[ystart:yend, xstart:xend, None],
                                    cell[ystart_cell:yend_cell, xstart_cell:xend_cell, None]), axis=2), axis=2)

    assert not np.any(np.isnan(pattern))
    pattern = np.asarray(pattern, dtype=bool)

    return pattern


def double_cell(cell,
                x,
                y,
                vec_a,
                vec_b,
                na: int = 1,
                nb: int = 0):
    """
    Create new unit cell by doubling the original one by a factor of na along vec_a and nb along vec_b

    :param np.array cell: initial cell
    :param list or np.array x: x-coordinates of cell
    :param list or np.array y: y-coordinates of cell
    :param list or np.array vec_a: periodicity vector a
    :param list or np.array vec_b: periodicity vector b
    :param na: number of times to double unit cell along vec_a
    :param nb: number of times to double cell along vec_b

    :return np.array big_cell: doubled cell
    :return np.array xs: x-coordinates of doubled cell
    :return np.array ys: y-coordinates of double cell
    """

    vec_a = np.array(vec_a, copy=True)
    vec_b = np.array(vec_b, copy=True)

    if not (na == 1 and nb == 0):
        big_cell = cell
        xs = x
        ys = y
        for ii in range(na):
            big_cell, xs, ys = double_cell(big_cell, xs, ys, 2**ii * vec_a, vec_b, na=1, nb=0)

        for jj in range(nb):
            big_cell, xs, ys = double_cell(big_cell, xs, ys, 2**jj * vec_b, 2**na * vec_a, na=1, nb=0)
    else:
        dyc, dxc = cell.shape

        v1 = np.array([0, 0])
        v2 = 2*vec_a
        v3 = vec_b
        v4 = 2*vec_a + vec_b

        xs = np.arange(np.min([v1[0], v2[0], v3[0], v4[0]]), np.max([v1[0], v2[0], v3[0], v4[0]]) + 1)
        ys = np.arange(np.min([v1[1], v2[1], v3[1], v4[1]]), np.max([v1[1], v2[1], v3[1], v4[1]]) + 1)

        dx = len(xs)
        dy = len(ys)

        big_cell = np.zeros((dy, dx)) * np.nan

        for n in [0, 1]:
            xzero, yzero = vec_a * n
            istart_x = int(xzero - np.min(xs) + np.min(x))
            istart_y = int(yzero - np.min(ys) + np.min(y))

            big_cell[istart_y:istart_y+dyc, istart_x:istart_x+dxc][np.logical_not(np.isnan(cell))] = \
                cell[np.logical_not(np.isnan(cell))]

    return big_cell, xs, ys


def get_sim_unit_cell(vec_a,
                      vec_b,
                      nphases: int):
    """
    Get unit cell, which can be repeated to form SIM pattern.

    :param list or np.array vec_a:
    :param list or np.array vec_b:
    :param int nphases: number of phase shifts. Required to determine the on and off pixels in cell.

    :return np.array cell: square array representing cell. Ones and zeroes give on and off points, and nans are
    points that are not part of the unit cell, but are necessary to pad the array to make it squares
    :return np.array x_cell: x-coordinates of cell pixels
    :return np.array y_cell: y-coordinates of cell pixels
    """

    # ensure both vec_b components are divisible by nphases
    if not float(vec_b[0] / nphases).is_integer() or not float(vec_b[1] / nphases).is_integer():
        raise ValueError("At least one component of vec_b was not divisible by nphases")

    # get full unit cell
    cell, x_cell, y_cell = get_unit_cell(vec_a, vec_b)
    # get reduced unit cell from vec_a, vec_b/nphases. If we set all of these positions to 1,
    # then we get perfect tiling.
    vec_b_sub = np.array(vec_b) / nphases
    cell_sub, x_cell_sub, y_cell_sub = get_unit_cell(vec_a, vec_b_sub)
    cell_sub[np.logical_not(np.isnan(cell_sub))] = 1

    iy_start, = np.where(np.array(y_cell) == np.min(y_cell_sub))
    iy_start = int(iy_start)
    iy_end = iy_start + cell_sub.shape[0]

    ix_start, = np.where(np.array(x_cell) == np.min(x_cell_sub))
    ix_start = int(ix_start)
    ix_end = ix_start + cell_sub.shape[1]

    # line up origins of the two cells
    cell[iy_start:iy_end, ix_start:ix_end] += np.nansum(
        np.concatenate((cell_sub[:, :, None], cell[iy_start:iy_end, ix_start:ix_end, None]), axis=2), axis=2)

    with np.errstate(invalid='ignore'):
        if np.nansum(cell) != np.sum(cell >= 0) / nphases:
            raise ValueError("Cell does not have appropriate number of 'on' pixels")

    return cell, x_cell, y_cell


def get_unit_cell(vec_a,
                  vec_b):
    """
    Generate a mask which represents one unit cell of a pattern for given vectors.
    This mask is a square array with NaNs at positions outside of the unit cell, and
    zeros at points in the cell.

    The unit cell is the area enclosed by [0, vec_a, vec_b, vec_a + vec_b]. For pixels, we say that
    an entire pixel is within the cell if its center is. For a pixel with center exactly on one of the
    edges of the cell, we say it is inside if it lies on the lines from [0, vec_b] or
    [0, vec_a] and outside of its lies on the lines from [vec_a, vec_a + vec_b] or [vec_b, vec_a + vec_b].
    This choice avoids including pixels twice.

    :param list or np.array vec_a: [dxa, dya]
    :param list or np.array vec_b: [dxb, dyb]

    :return np.array cell:
    :return np.array x:
    :return np.array y:
    """

    # test that vec_a and vec_b components are integers
    for vecs in [vec_a, vec_b]:
        for v in vec_a:
            if not float(v).is_integer():
                raise ValueError("At least one component of vec_a or vec_b cannot be interpreted as an integer")

    # copy vector data, so don't affect inputs
    vec_a = np.array(vec_a, copy=True, dtype=int)
    vec_b = np.array(vec_b, copy=True, dtype=int)

    # check vectors are linearly independent
    if np.cross(vec_a, vec_b) == 0:
        raise ValueError("vec_a and vec_b are linearly dependent.")

    # square array containing unit cell, with points not in unit cell nans
    dy = np.abs(vec_a[1]) + np.abs(vec_b[1])
    dx = np.abs(vec_a[0]) + np.abs(vec_b[0])

    # x-coordinates massaged so that origin is at x=0
    x = np.array(range(dx))
    if vec_a[0] < 0 and vec_b[0] >= 0:
        x = x + vec_a[0] + 1
    elif vec_a[0] >= 0 and vec_b[0] < 0:
        x = x + vec_b[0] + 1
    elif vec_a[0] < 0 and vec_b[0] < 0:
        x = x + vec_a[0] + vec_b[0] + 1

    # y-coordinates massaged so that origin is at y=0
    y = np.array(range(dy))
    if vec_a[1] < 0 and vec_b[1] >= 0:
        y = y + vec_a[1] + 1
    elif vec_a[1] >= 0 and vec_b[1] < 0:
        y = y + vec_b[1] + 1
    elif vec_a[1] < 0 and vec_b[1] < 0:
        y = y + vec_a[1] + vec_b[1] + 1

    xx, yy = np.meshgrid(x, y)

    # get cell volume from cross product
    cell_volume = np.abs(vec_a[0] * vec_b[1] - vec_a[1] * vec_b[0])

    # generate cell
    cell = np.array(test_in_cell([xx, yy], vec_a, vec_b), dtype=float)
    cell[cell == False] = np.nan
    cell[cell == True] = 0

    # check unit cell has correct volume
    assert np.nansum(np.logical_not(np.isnan(cell))) == cell_volume

    return cell, x, y


def test_in_cell(points,
                 va,
                 vb):
    """
    Test if points (x, y) are in the unit cell for a given pair of unit vectors. We suppose the
    unit cell is the region enclosed by 0, va, vb, and va + vb. Point on the boundary are considered
    inside if they are on the lines 0 -> va or 0 ->vb, and outside if they are on the lines va -> va+vb
    or vb -> va + vb

    :param points: [xx, yy]
    :param va:
    :param vb:
    :return:
    """

    va = np.array(va, copy=True)
    va = va.reshape([2, ])

    vb = np.array(vb, copy=True)
    vb = vb.reshape([2, ])

    x, y = points

    def line(x, p1, p2): return ((p2[1] - p1[1]) * x + p1[1] * p2[0] - p1[0] * p2[1]) / (p2[0] - p1[0])

    precision = 12

    # strategy: consider parellel lines from line1 = [0,0] -> va and line2 = vb -> va + vb
    # if point is on opposite sides of line1 and line2, or exactly on line1 then it is inside the cell
    # if it is one the same sides of line1 and line2, or exactly on line2, it is outside
    if va[0] != 0:
        gthan_a1 = np.round(line(x, [0, 0], va), precision) > np.round(y, precision)
        eq_a1 = np.round(line(x, [0, 0], va), precision) == np.round(y, precision)
        gthan_a2 = np.round(line(x, vb, va + vb), precision) > np.round(y, precision)
        eq_a2 = np.round(line(x, vb, va + vb), precision) == np.round(y, precision)
    else:
        # if x-component of va = 0. Then x-component of vb cannot be zero, else linearly dependent
        gthan_a1 = np.round(x, precision) > 0
        eq_a1 = np.round(x, precision) == 0
        gthan_a2 = np.round(x, precision) > np.round(vb[0], precision)
        eq_a2 = np.round(x, precision) == np.round(vb[0], precision)

    in_cell_a = np.logical_and(np.logical_or(gthan_a1 != gthan_a2, eq_a1), np.logical_not(eq_a2))

    # same strategy for vb
    if vb[0] != 0:
        gthan_b1 = np.round(line(x, [0, 0], vb), precision) > np.round(y, precision)
        eq_b1 = np.round(line(x, [0, 0], vb), precision) == np.round(y, precision)
        gthan_b2 = np.round(line(x, va, va + vb), precision) > np.round(y, precision)
        eq_b2 = np.round(line(x, va, va + vb), precision) == np.round(y, precision)
    else:
        # if x-component of vb = 0. Then x-component of va cannot be zero, else linearly dependent
        gthan_b1 = np.round(x, precision) > 0
        eq_b1 = np.round(x, precision) == 0
        gthan_b2 = np.round(x, precision) > np.round(va[0], precision)
        eq_b2 = np.round(x, precision) == np.round(va[0], precision)

    in_cell_b = np.logical_and(np.logical_or(gthan_b1 != gthan_b2, eq_b1), np.logical_not(eq_b2))

    in_cell = np.logical_and(in_cell_a, in_cell_b)

    return in_cell


def reduce2cell(point,
                va,
                vb):
    """
    Given a vector, reduce it to coordinates within the unit cell
    :param np.array point:
    :param list or np.array va:
    :param list or np.array vb:
    :return:
    """
    for vec in [va, vb]:
        for v in vec:
            if not float(v).is_integer():
                raise ValueError("at least one component of va or vb could nto be interpreted as an integer.")

    point = np.array(point, copy=True)
    va = np.array(va, copy=True, dtype=int)
    vb = np.array(vb, copy=True, dtype=int)

    ra, rb = get_reciprocal_vects(va, vb)
    # need to round to avoid problems with machine precision
    na_out = int(np.floor(np.round(np.vdot(point, ra), 12)))
    nb_out = int(np.floor(np.round(np.vdot(point, rb), 12)))
    point_red = point - (na_out * va + nb_out * vb)

    if not test_in_cell(point_red, va, vb):
        print(f"({point_red[0]:d}, {point_red[1]:d}) not in cell,"
              f" va=({va[0]:d}, {va[1]:d}),"
              f" vb=({vb[0]:d}, {vb[1]:d})")

    assert test_in_cell(point_red, va, vb)

    # # this point may not be in cell, but only need to go one away to find it
    # _, na, nb = get_closest_lattice_vec(point, va, vb)
    #
    # # todo: found [-1, 0, 1] not enough to ensure point is there
    # # e.g. vec_a = (-15, 15), vec_b = (-27, -30), point = (2, -12) requires going 2 away
    # # is this a rare case where there is a "tie" between "closest" vectors,
    # # or are there more pathological cases?
    # # e.g. vec_a = (-15, 15), vec_b = (-27, -30), point = (2, -11) requires going 3 away
    #
    # found_point = False
    # nmax = 1
    # while not found_point:
    #     # each time expand range, don't want to redo any points we already checked
    #     n1s, m1s = np.meshgrid([-nmax, nmax], range(-nmax, nmax+1))
    #     n2s, m2s = np.meshgrid(range(-(nmax - 1), nmax), [-nmax, nmax])
    #     ns = np.concatenate((n1s.ravel(), n2s.ravel()), axis=0)
    #     ms = np.concatenate((m1s.ravel(), m2s.ravel()), axis=0)
    #
    #     for n,m in zip(ns, ms):
    #         point_red = point - (na + n) * va - (nb + m) * vb
    #         #print("%d, %d, (%d, %d)" % (na+n, nb+m, point_red[0], point_red[1]))
    #
    #         if test_in_cell(point_red, va, vb):
    #             found_point = True
    #             na_out = na + n
    #             nb_out = nb + m
    #             break
    #
    #     nmax += 1
    #
    # if not found_point:
    #     raise Exception("did not find point (%d,%d) in unit cell of va=(%d,%d), vb=(%d,%d)" %
    #                     (point[0], point[1], va[0], va[1], vb[0], vb[1]))

    return point_red, na_out, nb_out


def convert_cell(cell1,
                 x1,
                 y1,
                 va1,
                 vb1,
                 va2,
                 vb2):
    """
    Given a unit cell described by vectors va1 and vb2, convert to equivalent description
    in terms of va2, vb2
    :param cell1:
    :param x1:
    :param y1:
    :param va1:
    :param vb1:
    :param va2:
    :param vb2:

    :return cell2, x2, y2:
    """
    # todo: add check that va1/vb1 and va2/vb2 describe same lattice
    for vec in [va1, vb1, va2, vb2]:
        for v in vec:
            if not float(v).is_integer():
                raise ValueError("At least one component of va1, vb1, va2, or vb2 could not be interpreted as an integer")

    cell2, x2, y2 = get_unit_cell(va2, vb2)
    y1min = y1.min()
    x1min = x1.min()

    for ii in range(cell2.shape[0]):
        for jj in range(cell2.shape[1]):
            p1, _, _ = reduce2cell((x2[jj], y2[ii]), va1, vb1)
            cell2[ii, jj] += cell1[p1[1] - y1min, p1[0] - x1min]

    return cell2, x2, y2


def get_minimal_cell(cell,
                     x,
                     y,
                     va,
                     vb):
    """
    Convert to cell using smallest lattice vectors
    :param cell:
    :param x:
    :param y:
    :param va:
    :param vb:
    :return cell_m, x_m, y_m, va_m, vb_m:
    """
    va_m, vb_m = reduce_basis(va, vb)
    cell_m, x_m, y_m = convert_cell(cell, x, y, va, vb, va_m, vb_m)
    return cell_m, x_m, y_m, va_m, vb_m


def show_cell(v1,
              v2,
              cell,
              x,
              y,
              **kwargs):
    """
    Plot unit cell and periodicity vectors

    :param list or np.array v1:
    :param list or np.array v2:
    :param np.array cell:
    :param list or np.array x:
    :param list or np.array y:

    :return figh: handle to resulting figure
    """

    v1 = np.array(v1, copy=True).ravel()
    v2 = np.array(v2, copy=True).ravel()

    if not v1.dtype.kind in np.typecodes["AllInteger"] or \
       not v2.dtype.kind in np.typecodes["AllInteger"]:
       raise ValueError(f"v1 and v2 had data types '{v1.dtype}' and '{v2.dtype}', but both be type 'int'")

    # plot
    fig = plt.figure(**kwargs)
    ax = fig.add_subplot(1, 1, 1)
    ax.set_title(f"Unit cell,"
                 f" $v_1$ = ({v1[0]:d}, {v1[1]:d}),"
                 f" $v_2$ = ({v2[0]:d}, {v2[1]:d})")

    ax.imshow(np.abs(cell), origin='lower', extent=[x[0] - 0.5, x[-1] + 0.5, y[0] - 0.5, y[-1] + 0.5])
    ax.plot([0, v1[0]], [0, v1[1]], 'r', label="$v_1$")
    ax.plot([0, v2[0]], [0, v2[1]], 'g', label="$v_2$")
    ax.plot([v2[0], v2[0] + v1[0]], [v2[1], v2[1] + v1[1]], 'r')
    ax.plot([v1[0], v2[0] + v1[0]], [v1[1], v2[1] + v1[1]], 'g')
    ax.legend()
    ax.set_xlabel("x")
    ax.set_ylabel("y")

    return fig


# determine parameters of SIM patterns
def get_reciprocal_vects(vec_a,
                         vec_b,
                         mode: str = 'frequency'):
    """
    Compute the reciprocal vectors for (real-space) lattice vectors vec_a and vec_b.
    exp[ i 2*pi*ai * bj] = 1

    If we call the lattice vectors a_i and the
    reciprocal vectors b_j, then these should be defined such that dot(a_i, b_j) = delta_{ij} if the b_j are frequency
    like, or dot(a_i, b_j) = 2*pi * delta_{ij} if the b_j are angular-frequency like.

    Cast this as matrix problem
    [[Ax, Ay]   *  [[R1_x, R2_x]   =  [[1, 0]
     [Bx, By]]      [R1_y, R2_y]]      [0, 1]]

    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param mode: 'frequency' or 'angular-frequency'

    :return np.array reciprocal_vect1:
    :return np.array reciprocal_vect2:
    """
    vec_a_temp = np.array(vec_a, copy=True)
    vec_a_temp = vec_a_temp.reshape([vec_a_temp.size, 1])

    vec_b_temp = np.array(vec_b, copy=True)
    vec_b_temp = vec_b_temp.reshape([vec_b_temp.size, 1])

    # best to check this directly, as sometimes due to numerical issues np.linalg.inv() will not throw error
    if np.cross(vec_a_temp[:, 0], vec_b_temp[:, 0]) == 0:
        raise ValueError("vec_a_temp and vec_b_temp are linearly dependent, "
                         "so their reciprocal vectors could not be computed.")

    a_mat = np.concatenate([vec_a_temp.transpose(), vec_b_temp.transpose()], 0)
    try:
        inv_a = np.linalg.inv(a_mat)
        reciprocal_vect1 = inv_a[:, 0][:, None]
        reciprocal_vect2 = inv_a[:, 1][:, None]
    except np.linalg.LinAlgError:
        raise ValueError("vec_a_temp and vec_b_temp are linearly dependent, "
                         "so their reciprocal vectors could not be computed.")

    if mode == 'angular-frequency':
        reciprocal_vect1 = reciprocal_vect1 * (2 * np.pi)
        reciprocal_vect2 = reciprocal_vect2 * (2 * np.pi)
    elif mode == 'frequency':
        pass
    else:
        raise ValueError("'mode' should be 'frequency' or 'angular-frequency', but was '%s'" % mode)

    return reciprocal_vect1, reciprocal_vect2


def get_sim_angle(vec_a,
                  vec_b):
    """
    Get angle of SIM pattern in
    :param list[int] or np.array vec_a: [vx, vy]
    :param list[int] or np.array vec_b: [vx, vy]

    :return angle: angle in radians
    """
    recp_va, recp_vb = get_reciprocal_vects(vec_a, vec_b)
    angle = np.angle(recp_vb[0, 0] + 1j * recp_vb[1, 0])

    return np.mod(angle, 2*np.pi)


def get_sim_period(vec_a,
                   vec_b):
    """
    Get period of SIM pattern constructed from periodicity vectors.

    The period is the distance between parallel lines pointing in the direction of vec_a passing through the
    points 0 and vec_b_temp respectively. We construct this by taking the projection of vec_b along the perpendicular to
    vec_a. NOTE: to say this another way, the period is given by the reciprocal lattice vector orthogonal to vec_a.

    :param list[int] or np.array vec_a: [vx, vy]
    :param list[int] or np.array vec_b: [vx, vy]

    :return period:
    """
    uvec_perp_a = np.array([vec_a[1], -vec_a[0]]) / np.sqrt(vec_a[0]**2 + vec_a[1]**2)

    # get period
    period = np.abs(uvec_perp_a.dot(vec_b))

    return period


def get_sim_frqs(vec_a,
                 vec_b):
    """
    Get spatial frequency of SIM pattern constructed from periodicity vectors.

    :param list[int] or np.array vec_a: [vx, vy]
    :param list[int] or np.array vec_b: [vx, vy]

    :return fx, fy:
    """
    recp_va, recp_vb = get_reciprocal_vects(vec_a, vec_b)
    fx = recp_vb[0, 0]
    fy = recp_vb[1, 0]

    return fx, fy


def get_sim_phase(vec_a,
                  vec_b,
                  nphases: int,
                  phase_index: int,
                  pattern_size: list[int],
                  origin: str = 'fft'):
    """
    Get phase of dominant frequency component in the SIM pattern.

    P(x, y) = 0.5 * (1 + cos(2pi*f_x*x + 2pi*f_y*y + phi)

    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param nphases: number of equal phase shifts for SIM pattern
    :param phase_index: 0, ..., nphases-1
    :param pattern_size: [nx, ny]
    :param origin: origin to use for computing the phase. If 'fft', will assume the coordinates are the same
    as used in an FFT (i.e. before performing an ifftshift, with the 0 near the center). If 'corner', will
    suppose the origin is at pattern[0, 0].

    :return phase: phase of the SIM pattern at the dominant frequency component (which is recp_vec_b)
    """

    cell, xs, ys = get_sim_unit_cell(vec_a, vec_b, nphases)
    fourier_component, _ = get_pattern_fourier_component(cell, xs, ys, vec_a, vec_b, 0, 1,
                                                         nphases, phase_index, origin, pattern_size)

    phase = np.angle(fourier_component)

    return np.mod(phase, 2*np.pi)


def get_lattice_dft_frqs(vec_a,
                         vec_b):
    """
    Get the minimal number of Fourier frequencies which determine a periodic pattern.

    Since the pattern is periodic and binary, it can be described by a generalization of the DFT. Here the indices of
    of the pattern are given by the pixel locations in the unit cell. The frequencies are also defined on a unit cell,
    but in this case generated by the unit vectors b1 * det(A) and b2 * det(A), where det(A) is the determinant of a
    matrix with rows or columns given by the periodicity vectors a1, a2.

    LDFT = lattice DFT
    LDFT[g](f1, f2) = \sum_{nx, ny} exp[-2*np.pi*1j * [f1 * b1 * (nx, ny) + f2 * b2 * (nx, ny)]] * g(n_x, n_y)
    Instead of (nx, ny), one can also work with coefficients d1, d2 for the vectors a1 and a2.
    i.e. (nx, ny) = d1(nx, ny) * a1 + d2(nx, ny) * a2

    :param vec_a:
    :param vec_b:
    :return bcell, f1, f2, fvecs: bcell is a boolean array indicating which points are in the frequency unit cell.
    f1 and f2 are integers which define the frequencies, f = f1 * b1 + f2 * b2. fvecs are the frequencies for each
    points in bcell
    """
    b1, b2 = get_reciprocal_vects(vec_a, vec_b)
    det = int(np.linalg.det(np.stack((vec_a, vec_b), axis=1)))

    bcell, f1, f2 = get_unit_cell(b1.ravel() * det, b2.ravel() * det)
    bcell[bcell == 0] = 1
    bcell[np.isnan(bcell)] = 0
    bcell = bcell.astype(bool)

    fvecs = np.expand_dims(f1, axis=(0, 2)) * np.expand_dims(b1.ravel(), axis=(0, 1)) + \
            np.expand_dims(f2, axis=(1, 2)) * np.expand_dims(b2.ravel(), axis=(0, 1))

    return bcell, f1, f2, fvecs


def get_lattice_dft(unit_cell,
                    x,
                    y,
                    vec_a,
                    vec_b):
    """
    Compute the lattice DFT of a given pattern defined on a unit cell
    @param unit_cell:
    @param x: coordinates of the unit cell
    @param y:
    @param vec_a: lattice vectors
    @param vec_b:
    @return ldft, f1, f2, fvecs:
    """
    bcell, f1, f2, fvecs = get_lattice_dft_frqs(vec_a, vec_b)

    bcell = bcell.astype(float)
    bcell[bcell == 0] = np.nan

    xx, yy = np.meshgrid(x, y)
    ldft = np.zeros(bcell.shape, dtype=complex) * np.nan
    for ii in range(ldft.shape[0]):
        for jj in range(ldft.shape[1]):
            if np.isnan(bcell[ii, jj]):
                continue
            ldft[ii, jj] = np.nansum(unit_cell * np.exp(-1j*2*np.pi * (fvecs[ii, jj, 0] * xx + fvecs[ii, jj, 1] * yy)))

    return ldft, f1, f2, fvecs


def get_pattern_fourier_component(unit_cell,
                                  x,
                                  y,
                                  vec_a,
                                  vec_b,
                                  na: int,
                                  nb: int,
                                  nphases: int = 3,
                                  phase_index: int = 0,
                                  origin: str = 'fft',
                                  dmd_size=None):
    """
    Get fourier component at f = n * recp_vec_a + m * recp_vec_b.

    ft(f) = \sum_r f(r) * exp(-1j * 2*pi * f * r)

    :param np.array unit_cell: unit cell, as produced by get_sim_unit_cell()
    :param list[int] or np.array x: x-coordinates of unit cell
    :param list[int] or np.array y: y-coordinates of unit cell
    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param na: integer multiples of recp_vec_a
    :param nb: integer multiples of recp_vec_b
    :param nphases: only relevant for calculating phase
    :param phase_index: only relevant for calculating phase
    :param origin: "corner" or "fft". Specifies where the origin of the
    :param dmd_size: [nx, ny], only required if origin is "fft"

    :return complex fcomponent: fourier component of pattern at frq_vector
    :return np.array frq_vector: recp_vec_a * n + recp_vec_b * m
    """

    recp_vect_a, recp_vect_b = get_reciprocal_vects(vec_a, vec_b, mode='frequency')
    frq_vector = na * recp_vect_a + nb * recp_vect_b

    # fourier component is integral over unit cell
    xxs, yys = np.meshgrid(x, y)
    fcomponent = np.nansum(unit_cell * np.exp(-1j*2*np.pi * (frq_vector[0] * xxs + frq_vector[1] * yys)))

    # correct phase for start coord
    start_coord = np.array(vec_b) / nphases * phase_index
    phase = np.angle(fcomponent) - 2 * np.pi * start_coord.dot(frq_vector)

    if origin == 'corner':
        pass
    elif origin == 'fft':
        if dmd_size is None:
            raise TypeError("dmd_size was None, but must be specified when origin is 'fft'")

        # now correct for
        nx, ny = dmd_size
        # x_pattern = tools.get_fft_pos(nx)
        # y_pattern = tools.get_fft_pos(ny)
        x_pattern = np.arange(nx) - (nx // 2)
        y_pattern = np.arange(ny) - (ny // 2)
        # center coordinate in the edge coordinate system
        center_coord = np.array([-x_pattern[0], -y_pattern[0]])

        phase = phase + 2 * np.pi * center_coord.dot(frq_vector)

    else:
        raise ValueError(f"origin must be 'corner' or 'fft', but was '{origin:s}'")

    fcomponent = np.abs(fcomponent) * np.exp(1j * phase)

    return fcomponent, frq_vector


def get_efield_fourier_components(unit_cell,
                                  x,
                                  y,
                                  vec_a,
                                  vec_b,
                                  nphases: int,
                                  phase_index: int,
                                  dmd_size: list[int],
                                  nmax: int = 20,
                                  origin: str = "fft",
                                  otf=None):
    """
    Generate many Fourier components of pattern

    :param unit_cell:
    :param x:
    :param y:
    :param vec_a:
    :param vec_b:
    :param nphases:
    :param phase_index:
    :param dmd_size:
    :param nmax:
    :param origin:
    :param otf: optical transfer function to apply

    :return efield: evaluated at the frequencyes vecs = ns * recp_va + ms * recp_vb
    :return ns:
    :return ms:
    :return vecs:
    """

    if otf is None:
        def otf(fx, fy): return 1

    rva, rvb = get_reciprocal_vects(vec_a, vec_b)

    # first, get electric field fourier components
    ns = np.arange(-nmax, nmax + 1)
    ms = np.arange(-nmax, nmax + 1)
    ninds = 2 * nmax + 1
    vecs = np.zeros((ninds, ninds, 2))
    efield_fc = np.zeros((ninds, ninds), dtype=complex)

    # calculate half of values, as can get other half with E(-f) = E^*(f)
    for ii in range(nmax, len(ns)):
        for jj in range(len(ms)):

            # maximum pattern size is f = 0.5 1/mirrors, after this Fourier transform repeats information
            v = rva * ns[ii] + rvb * ms[jj]
            # if np.linalg.norm(v) > 1:
            # if np.linalg.norm(v) > 0.5:
            if np.abs(v[0]) > 0.5 or np.abs(v[1]) > 0.5:
                efield_fc[ii, jj] = 0
                vecs[ii, jj] = v[:, 0]
            else:
                efield_fc[ii, jj], v = get_pattern_fourier_component(unit_cell, x, y, vec_a, vec_b, ns[ii], ms[jj],
                                                                     nphases, phase_index, origin=origin,
                                                                     dmd_size=dmd_size)
                vecs[ii, jj] = v[:, 0]

    # E(-f) = E^*(f)
    efield_fc[:nmax] = np.flip(efield_fc[nmax + 1:], axis=(0, 1)).conj()
    vecs[:nmax] = -np.flip(vecs[nmax + 1:], axis=(0, 1))

    # apply OTF
    efield_fc = efield_fc * otf(vecs[:, :, 0], vecs[:, :, 1])

    # divide by volume of unit cell (i.e. maximum possible Fourier component)
    with np.errstate(invalid='ignore'):
        efield_fc = efield_fc / np.nansum(unit_cell >= 0)

    return efield_fc, ns, ms, vecs


def get_int_fc(efield_fc):
    """
    Generate intensity fourier components from efield fourier components

    :param efield_fc: electric field Fourier components nvec1 x nvec2 array,
     where efield_fc[ii, jj] is the electric field at frequencies f = ii * v1 + jj * v2.

    :return intensity_fc: intensity Fourier components at the same frequencies, f = ii * v1 + jj * v2
    """
    ny, nx = efield_fc.shape
    if np.mod(ny, 2) == 0 or np.mod(nx, 2) == 0:
        # TODO: the flip operation only works for taking f-> -f only works assuming that array size is odd, with f=0 at the center
        raise ValueError("not implemented for even sized arrays")

    # I(f) = autocorrelation[E(f)] = convolution[E(f), E^*(-f)]
    intensity_fc = scipy.signal.fftconvolve(efield_fc, np.flip(efield_fc, axis=(0, 1)).conj(), mode='same')

    return intensity_fc


# other fourier component function
def get_intensity_fourier_components(unit_cell,
                                     x,
                                     y,
                                     vec_a,
                                     vec_b,
                                     fmax: float,
                                     nphases: int,
                                     phase_index: int,
                                     dmd_size: list[int],
                                     nmax: int = 20,
                                     origin: str = "fft",
                                     include_blaze_correction: bool = True,
                                     dmd_params: dict = None):
    """
    Utility function for computing many electric field and intensity components of the Fourier pattern, including the
    effect of the Blaze angle and system numerical aperture

    # todo: deprecate this in favor of get_int_fc() and get_efield_fourier_components()
    # todo: instead of setting nmax, just generate all e-field components that do not get blocked
    # todo: debating moving this function to simulate_dmd.py instead

    Given an electric field in fourier space E(k), the intensity I(k) = \sum_q E(q) E^*(q-k).
    For a pattern where P(r)^2 = P(r), these must be equal, giving P(k) = \sum_q P(q) P(q-k).
    But the relevant quantity after passing through the microscope is P(k) * bandlimit(k), where bandlimit(k) = 1 for
    k <= fmax, and 0 otherwise. Then the intensity pattern should be
    \sum_q P(q) P(q-k) * bandlimit(q) * bandlimit(q-k)

    :param np.array unit_cell: unit cell
    :param list[int] or np.array x: x-coordinates of unit cell
    :param list[int] or np.array y: y-coordinates of unit cell
    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param fmax: maximum pass frequency for electric field in 1/mirrors. i.e. fmax = NA/lambda without the factor
    of 2 that appears for the intensity. Note that fmax <= 1, which is the maximum frequency supported by the DMD.
    :param nphases:
    :param phase_index:
    :param dmd_size: [nx, ny]
    :param nmax:
    :param origin: origin used to compute pattern phases "fft" or ""
    :param include_blaze_correction: if True, include blaze corrections
    :param dmd_params: dictionary {'wavelength', 'dx', 'dy', 'wx', 'wy', 'theta_ins': [tx_in, ty_in],
     'theta_outs': [tx_out, ty_out]}

    :return np.array intensity_fc: fourier components of intensity (band limited)
    :return np.array efield_fc: fourier components of efield (band limited)
    :return np.array ns: vec = ns * recp_vec_a + ms * recp_vec_b
    :return np.array ms: vec = ns * recp_vec_a + ms * recp_vec_b
    :return np.array vecs: ns * recp_vec_a + ms * recp_vec_b
    """

    if dmd_params is None and include_blaze_correction is True:
        raise ValueError("dmd_params must be supplied as include_blaze_correction is True")

    if dmd_params is not None:
        wavelength = dmd_params["wavelength"]
        gamma = dmd_params["gamma"]
        dx = dmd_params["dx"]
        dy = dmd_params["dy"]
        wx = dmd_params["wx"]
        wy = dmd_params["wy"]
        tin_x, tin_y = dmd_params['theta_ins']
        tout_x, tout_y = dmd_params['theta_outs']

    # get minimal lattice vectors
    # todo: use minimal lattice vectors to do the computation
    # va_m, vb_m = reduce_basis(vec_a, vec_b)
    # cell_m, x_m, y_m = convert_cell(unit_cell, x, y, vec_a, vec_b, va_m, vb_m)

    # todo: compute nmax

    # first, get electric field fourier components
    ns = np.arange(-nmax, nmax + 1)
    ms = np.arange(-nmax, nmax + 1)
    vecs = np.zeros((len(ns), len(ms), 2))
    efield_fc = np.zeros((len(ns), len(ms)), dtype=complex)
    blaze_envelope = np.zeros(efield_fc.shape)

    # todo: calculating at f and -f is redundant
    for ii in range(len(ns)):
        for jj in range(len(ms)):
            efield_fc[ii, jj], v = get_pattern_fourier_component(unit_cell, x, y, vec_a, vec_b, ns[ii], ms[jj],
                                                                 nphases, phase_index, origin=origin, dmd_size=dmd_size)
            vecs[ii, jj] = v[:, 0]

            if include_blaze_correction:
                # wavelength * frq = theta in Fraunhofer approximation
                uvec_in = simulate_dmd.xy2uvector(tin_x, tin_y, "in")
                uvec_out = simulate_dmd.xy2uvector(tout_x + wavelength * vecs[ii, jj][0] / dx,
                                                   tout_y + wavelength * vecs[ii, jj][1] / dy, "out")
                # amb = uvec_in - uvec_out
                bma = uvec_out - uvec_in
                blaze_envelope[ii, jj] = simulate_dmd.blaze_envelope(wavelength, gamma, wx, wy, bma)

                efield_fc[ii, jj] = efield_fc[ii, jj] * blaze_envelope[ii, jj]


    # divide by volume of unit cell (i.e. maximum possible Fourier component)
    with np.errstate(invalid='ignore'):
        efield_fc = efield_fc / np.nansum(unit_cell >= 0)

    # band limit
    frqs = np.linalg.norm(vecs, axis=2)
    # enforce maximum allowable frequency from DMD
    efield_fc = efield_fc * (frqs <= 0.5)
    # enforce maximum allowable frequency from imaging system
    efield_fc = efield_fc * (frqs <= fmax)

    # I(f) = autocorrelation[E(f)] = convolution[E(f), E^*(-f)]
    # note: the flip operation only for taking f-> -f only works assuming that array size is odd, with f=0 at the center
    intensity_fc = scipy.signal.fftconvolve(efield_fc, np.flip(efield_fc, axis=(0, 1)).conj(), mode='same')
    # enforce maximum allowable frequency (should only be machine precision errors)
    intensity_fc = intensity_fc * (frqs <= 1)
    intensity_fc = intensity_fc * (frqs <= 2*fmax)

    return intensity_fc, efield_fc, ns, ms, vecs


def get_intensity_fourier_components_xform(pattern,
                                           affine_xform,
                                           roi: list[int],
                                           vec_a,
                                           vec_b,
                                           fmax: float,
                                           nmax: int = 20,
                                           cam_size=(2048, 2048),
                                           include_blaze_correction: bool = True,
                                           dmd_params: dict = None):
    """
    Utility function for computing many electric field and intensity components of the Fourier pattern, including the
    effect of the Blaze angle and system numerical aperture. To correct for ROI effects, extract from affine transformed
    pattern

    # todo: instead of setting nmax, just generate all e-field components that do not get blocked
    # todo: debating moving this function to simulate_dmd.py instead

    Given an electric field in fourier space E(k), the intensity I(k) = \sum_q E(q) E^*(q-k).
    For a pattern where P(r)^2 = P(r), these must be equal, giving P(k) = \sum_q P(q) P(q-k).
    But the relevant quantity after passing through the microscope is P(k) * bandlimit(k), where bandlimit(k) = 1 for
    k <= fmax, and 0 otherwise. Then the intensity pattern should be
    \sum_q P(q) P(q-k) * bandlimit(q) * bandlimit(q-k)

    :param np.array pattern:
    :param np.array affine_xform:
    :param list[int] roi:
    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param float fmax:
    :param int nmax:
    :param tuple[int] cam_size: (ny, nx)
    :param bool include_blaze_correction: if True, include blaze corrections
    :param dict dmd_params: dictionary {'wavelength', 'dx', 'dy', 'wx', 'wy', 'theta_ins': [tx_in, ty_in],
     'theta_outs': [tx_out, ty_out]}

    :return:
    """

    if dmd_params is None and include_blaze_correction is True:
        raise ValueError("dmd_params must be supplied as include_blaze_correction is True")

    if dmd_params is not None:
        wavelength = dmd_params["wavelength"]
        gamma = dmd_params["gamma"]
        dx = dmd_params["dx"]
        dy = dmd_params["dy"]
        wx = dmd_params["wx"]
        wy = dmd_params["wy"]
        tin_x, tin_y = dmd_params['theta_ins']
        tout_x, tout_y = dmd_params['theta_outs']

    recp_va, recp_vb = get_reciprocal_vects(vec_a, vec_b)

    # todo: generate roi directly instead of cropping
    # img_coords = np.meshgrid(range(cam_size[1]), range(cam_size[0]))
    xform_roi = affine.xform_shift_center(affine_xform, cimg_new=(roi[2], roi[0]))
    nx_roi = roi[3] - roi[2]
    ny_roi = roi[1] - roi[0]
    img_coords_roi = np.meshgrid(range(nx_roi), range(ny_roi))
    pattern_xformed = affine.xform_mat(pattern, xform_roi, img_coords_roi, mode="interp")
    # pattern_xformed = affine.affine_xform_mat(pattern, affine_xform, img_coords, mode="interp")
    # pattern_xformed = pattern_xformed[roi[0]:roi[1], roi[2]:roi[3]]
    pattern_xformed_ft = fft.fftshift(fft.fft2(fft.ifftshift(pattern_xformed)))

    fxs = fft.fftshift(fft.fftfreq(pattern_xformed.shape[1], 1))
    fys = fft.fftshift(fft.fftfreq(pattern_xformed.shape[0], 1))

    # first, get electric field fourier components
    ns = np.arange(-nmax, nmax + 1)
    ms = np.arange(-nmax, nmax + 1)
    vecs = np.zeros((len(ns), len(ms), 2))
    vecs_xformed = np.zeros(vecs.shape)

    efield_fc_xformed = np.zeros((len(ns), len(ms)), dtype=complex)
    blaze_envelope = np.zeros(efield_fc_xformed.shape)

    # todo: calculating @ freq and -freq is redundant
    for ii in range(len(ns)):
        for jj in range(len(ms)):
            vecs[ii, jj] = ns[ii] * recp_va[:, 0] + ms[jj] * recp_vb[:, 0]
            vecs_xformed[ii, jj, 0], vecs_xformed[ii, jj, 1], _ = \
                affine.xform_sinusoid_params(vecs[ii, jj, 0], vecs[ii, jj, 1], 0, affine_xform)

            try:
                efield_fc_xformed[ii, jj] = tools.get_peak_value(pattern_xformed_ft, fxs, fys,
                                                                 vecs_xformed[ii, jj], peak_pixel_size=2)
            except:  # todo: what exception is this supposed to catch?
                efield_fc_xformed[ii, jj] = 0

            if include_blaze_correction:
                # wavelength * frq = theta in Fraunhofer approximation
                uvec_in = simulate_dmd.xy2uvector(tin_x, tin_y, "in")
                uvec_out = simulate_dmd.xy2uvector(tout_x + wavelength * vecs[ii, jj][0] / dx,
                                                   tout_y + wavelength * vecs[ii, jj][1] / dy, "out")
                # amb = uvec_in - uvec_out
                bma = uvec_out - uvec_in
                blaze_envelope[ii, jj] = simulate_dmd.blaze_envelope(wavelength, gamma, wx, wy, bma)

                efield_fc_xformed[ii, jj] = efield_fc_xformed[ii, jj] * blaze_envelope[ii, jj]

    # divide by DC component
    efield_fc_xformed = efield_fc_xformed / np.max(np.abs(efield_fc_xformed))
    # hack to get to agree with nphases = 3
    # todo: why is this here?
    efield_fc_xformed = efield_fc_xformed / 3

    # band limit
    frqs = np.linalg.norm(vecs, axis=2)
    efield_fc_xformed = efield_fc_xformed * (frqs <= 0.5)
    efield_fc_xformed = efield_fc_xformed * (frqs <= fmax)

    # intensity fourier components from autocorrelation
    # intensity_fc_xformed = scipy.signal.fftconvolve(efield_fc_xformed, efield_fc_xformed, mode='same')
    intensity_fc_xformed = scipy.signal.fftconvolve(efield_fc_xformed,
                                                    np.flip(efield_fc_xformed, axis=(0, 1)).conj(),
                                                    mode='same')
    intensity_fc_xformed = intensity_fc_xformed * (frqs <= 1)
    intensity_fc_xformed = intensity_fc_xformed * (frqs <= 2*fmax)

    return intensity_fc_xformed, efield_fc_xformed, ns, ms, vecs, vecs_xformed


def show_fourier_components(vec_a,
                            vec_b,
                            fmax: float,
                            int_fc,
                            efield_fc,
                            ns,
                            ms,
                            vecs,
                            plot_lims: tuple[float] = (1e-4, 1),
                            gamma: float = 0.1,
                            figsize=(20, 10),
                            **kwargs):
    """
    Display strength of fourier components for a given pattern. Display function for data generated with
    ``get_bandlimited_fourier_components()''. See that function for more information about parameters

    :param list[int] or np.array vec_a:
    :param list[int] or np.array vec_b:
    :param fmax: maximum frequency for electric field
    :param np.array int_fc:
    :param np.array efield_fc:
    :param np.array ns:
    :param np.array ms:
    :param np.array vecs:
    :param plot_lims: limits in plots
    :param gamma: gamma to use in power law normalization of plots
    :param figsize:
    :param kwargs: passed through to figure

    :return figh: handle to figure
    """

    recp_va, recp_vb = get_reciprocal_vects(vec_a, vec_b)
    recp_va_reduced, recp_vb_reduced = reduce_recp_basis(vec_a, vec_b)

    # norm to use when plotting
    ft_norm = PowerNorm(vmin=plot_lims[0], vmax=plot_lims[1], gamma=gamma)

    # ################################
    # plot results
    # ################################

    figh = plt.figure(figsize=figsize, **kwargs)
    grid = figh.add_gridspec(2, 6, wspace=0.4)
    figh.suptitle(f"Pattern fourier weights versus position and reciprocal lattice vector\n"
                  f" va=({vec_a[0]:d}, {vec_a[1]:d});"
                  f" vb=({vec_b[0]:d}, {vec_b[1]}),"
                  f" max efield frq=1/{1/fmax:.2f} 1/mirrors")

    marker_size = 2

    # ################################
    # electric fields
    # ################################
    # fourier components scatter plot
    ax = figh.add_subplot(grid[0, :2])
    ax.set_facecolor((0., 0., 0.))
    ax.axis('equal')

    im = ax.scatter(vecs[:, :, 0].ravel(), vecs[:, :, 1].ravel(),
                    s=marker_size,
                    c=np.abs(efield_fc).ravel(), norm=ft_norm)

    ax.scatter([recp_va[0], recp_vb[0]], [recp_va[1], recp_vb[1]], edgecolor='r', facecolor='none')
    ax.scatter([recp_va_reduced[0], recp_vb_reduced[0]], [recp_va_reduced[1], recp_vb_reduced[1]],
               edgecolor="m", facecolor="none")
    ax.add_artist(Circle((0, 0), radius=fmax, color='r', fill=0, ls='-'))

    ax.set_xlim([-fmax, fmax])
    ax.set_ylim([-fmax, fmax])
    cb = plt.colorbar(im)

    ax.set_xlabel('$f_x$ (1/mirror)')
    ax.set_ylabel('$f_y$ (1/mirror)')
    cb.set_label('|FT(f)|')
    ax.set_title('efield versus freq')

    # fourier components image
    ax = figh.add_subplot(grid[0, 2:4])
    im = ax.imshow(np.abs(efield_fc), extent=[ns[0] - 0.5, ns[-1] + 0.5, ms[-1] + 0.5, ms[0] - 0.5],
                   norm=ft_norm)
    ax.set_xlabel("$n_1 v_1$ ($n_1$)")
    ax.set_ylabel("$n_2 v_2$ ($n_2$)")
    cb = plt.colorbar(im)
    cb.set_label('|FT(f)|')
    ax.set_title('efield versus recp vect')

    # ################################
    # intensity
    # ################################
    ax = figh.add_subplot(grid[1, :2])
    ax.set_facecolor((0., 0., 0.))
    ax.axis('equal')

    im = ax.scatter(vecs[:, :, 0].ravel(), vecs[:, :, 1].ravel(), s=marker_size,
                    c=np.abs(int_fc).ravel(), norm=ft_norm)
    ax.scatter([recp_va[0], recp_vb[0]], [recp_va[1], recp_vb[1]], edgecolor='r', facecolor='none')
    ax.scatter([recp_va_reduced[0], recp_vb_reduced[0]], [recp_va_reduced[1], recp_vb_reduced[1]],
               edgecolor="m", facecolor="none")

    ax.add_artist(Circle((0, 0), radius=(2*fmax), color='r', fill=0, ls='-'))
    ax.add_artist(Circle((0, 0), radius=fmax, color='r', fill=0, ls='-'))

    cb = plt.colorbar(im)
    ax.set_xlim([-2*fmax, 2*fmax])
    ax.set_ylim([-2*fmax, 2*fmax])

    ax.set_xlabel('$f_x$ (1/mirror)')
    ax.set_ylabel('$f_y$ (1/mirror)')
    cb.set_label('|FT(f)|')
    ax.set_title('intensity versus freq')

    # intensity image
    ax = figh.add_subplot(grid[1, 2:4])
    im = ax.imshow(np.abs(int_fc), extent=[ns[0] - 0.5, ns[-1] + 0.5, ms[-1] + 0.5, ms[0] - 0.5],
                   norm=ft_norm)
    cb = plt.colorbar(im)
    ax.set_xlabel('recp vec as')
    ax.set_ylabel('recp vec bs')
    cb.set_label('|FT(f)|')
    ax.set_title('intensity versus recp vect')

    # ################################
    # 1D plots
    # ################################
    # efield and intensity 1D
    ax = figh.add_subplot(grid[0, 4:])
    ax.set_title("|FT| vs frq")
    ax.set_xlabel("$f$ (1/mirrors)")

    # only plot one of +/- f, and only plot if above certain threshold
    vec_mag = np.linalg.norm(vecs, axis=-1)
    nmax1 = int(np.round(0.5 * (int_fc.shape[0] - 1)))
    nmax2 = int(np.round(0.5 * (int_fc.shape[1] - 1)))
    to_use = np.ones(int_fc.shape, dtype=int)
    xx, yy = np.meshgrid(range(-nmax2, nmax2 + 1), range(-nmax1, nmax1 + 1))
    to_use[xx > yy] = 0
    to_use[np.logical_and(xx == yy, yy < 0)] = 0

    to_plot_int = np.logical_and(to_use, np.abs(int_fc) >= plot_lims[0])
    to_plot_e = np.logical_and(to_plot_int, vec_mag <= fmax)

    ylim = [plot_lims[0], plot_lims[1] * 1.2]
    ax.plot([fmax, fmax], ylim, 'k')
    ax.plot([2*fmax, 2*fmax], ylim, 'k')

    ax.plot(vec_mag[to_plot_int], np.abs(int_fc[to_plot_int]), '.', label="I")
    ax.plot(vec_mag[to_plot_e], np.abs(efield_fc[to_plot_e]), 'x', label="E")
    ax.set_yscale("log")

    ax.set_ylim(ylim)
    ax.set_xlim([-0.1 * 2 * fmax, 2.2 * fmax])

    ax.legend()

    # E/I phases 1D
    ax = figh.add_subplot(grid[1, 4:])
    ax.set_title("Fourier component phase vs frq")
    ax.set_xlabel("Frequency (1/mirrors)")

    ylim = [-np.pi - 0.2, np.pi + 0.2]
    ax.plot([fmax, fmax], ylim, 'k')
    ax.plot([2 * fmax, 2 * fmax], ylim, 'k')

    ax.plot(vec_mag[to_plot_int], np.angle(int_fc[to_plot_int]), '.', label="I")
    ax.plot(vec_mag[to_plot_e], np.angle(efield_fc[to_plot_e]), 'x', label="E")

    ax.set_ylim(ylim)
    ax.set_xlim([-0.1 * 2 * fmax, 2.2 * fmax])

    ax.legend()

    return figh


# Lagrange-Gauss basis reduction
def reduce_basis(va,
                 vb):
    """
    Find the "smallest" set of basis vectors using Lagrange-Gauss basis reduction.

    :param va:
    :param vb:
    :return:
    """
    va = np.array(va, copy=True)
    va = va.reshape([2, ])

    vb = np.array(vb, copy=True)
    vb = vb.reshape([2, ])

    Ba = np.linalg.norm(va)**2
    mu = np.vdot(va, vb) / Ba
    vb = vb - np.round(mu) * va
    Bb = np.linalg.norm(vb)**2

    swapped = -1
    while Bb < Ba:
        va, vb = vb, va
        swapped *= -1

        Ba = Bb

        mu = np.inner(va, vb) / Ba
        vb = vb - np.round(mu) * va
        Bb = np.linalg.norm(vb) ** 2

    if swapped == 1:
        va, vb = vb, va

    return va, vb


def reduce_recp_basis(va,
                      vb):
    """
    Compute the shortest pair of reciprocal basis vectors. These vectors may not be dual to the lattice vectors
    in the sense that vi * rsj = delta_{ij}, but they do form a basis for the reciprocal lattice vectors.

    :param list or np.array va: lattice vector
    :param list or np.array vb:
    :return np.array rsa: reduced reciprocal vector a
    :return np.array rsb: reduced reciprocal vector b

    """

    va, vb = reduce_basis(va, vb)
    rsa, rsb = get_reciprocal_vects(va, vb)

    return rsa, rsb


def get_closest_lattice_vec(point,
                            va,
                            vb):
    """
    Find the closest lattice vector to point

    :param list or np.array point:
    :param list or np.array va:
    :param list or np.array vb:
    :return int na_min:
    :return int nb_min:
    :return float diff:
    """
    point = np.array(point, copy=True)
    point = point.reshape([2, ])

    # get reduced lattice basis vectors
    var, vbr = reduce_basis(va, vb)

    # get reduced reciprocal vectors
    rva, rvb = get_reciprocal_vects(var, vbr)
    frac_a = np.vdot(point, rva)
    nas = [int(np.ceil(frac_a)), int(np.floor(frac_a))]

    frac_b = np.vdot(point, rvb)
    nbs = [int(np.ceil(frac_b)), int(np.floor(frac_b))]

    # possible choices
    diff = np.inf
    for na in nas:
        for nb in nbs:
            v_diff = point - na * var - nb * vbr
            diff_current = np.linalg.norm(v_diff)

            if diff_current < diff:
                nar_min = na
                nbr_min = nb
                diff = diff_current
                vec = na*var + nb*vbr

    # convert back to initial basis lattice vectors
    # get reciprocal vectors
    ra, rb = get_reciprocal_vects(va, vb)
    # and how they are related to initial lattice vectors
    var_ints = np.array([np.vdot(var, ra), np.vdot(var, rb)])
    vbr_ints = np.array([np.vdot(vbr, ra), np.vdot(vbr, rb)])

    na_min = int(np.round(nar_min * var_ints[0] + nbr_min * vbr_ints[0]))
    nb_min = int(np.round(nar_min * var_ints[1] + nbr_min * vbr_ints[1]))

    return vec, na_min, nb_min


def get_closest_recip_vec(recp_point,
                          va,
                          vb):
    """
    Find the closest reciprocal lattive vector, f = na * rva + nb * rvb, to a given point in reciprocal space,
    recp_point.

    :param list or np.array recp_point:
    :param list or np.array va:
    :param list or np.array vb:

    :return np.array vec: na * rva + nb * rvb
    :return int na_min: na
    :return int nb_min: nb
    """

    recp_point = np.array(recp_point, copy=True)
    recp_point = recp_point.reshape([2, ])

    va = np.array(va, copy=True)
    va = va.reshape([2, ])

    vb = np.array(vb, copy=True)
    vb = vb.reshape([2, ])

    det = va[0] * vb[1] - va[1] * vb[0]

    rva, rvb = get_reciprocal_vects(va, vb)

    # use get_closest_lattice_vec() function after scaling rva, rvb to have integer components
    vec, na_min, nb_min = get_closest_lattice_vec(recp_point * det, rva * det, rvb * det)
    vec = vec / det

    return vec, na_min, nb_min


# working with grayscale patterns
def binarize(pattern_gray,
             mode: str = "floyd-steinberg"):
    """
    Binarize a gray scale pattern

    :param np.array pattern_gray: gray scale pattern, with values in the range [0, 1]
    :param str mode: "floyd-steinberg" to specify the Floyd-Steinberg error diffusion algorithm, "jjn" to use
    the error diffusion algorithm of Jarvis, Judis, and Ninke https:doi.org/10.1016/S0146-664X(76)80003-2,
     "random" to use a random dither, or "round" to round to the nearest value

    :return np.array pattern_binary: binary approximation of pattern_gray
    """

    pattern_gray = copy.deepcopy(pattern_gray)

    if np.any(pattern_gray) > 1 or np.any(pattern_gray) < 0:
        raise ValueError("pattern values must be in [0, 1]")

    ny, nx = pattern_gray.shape

    if mode == "floyd-steinberg":
        # error diffusion Kernel =
        # 1/16 * [[_ # 7], [3, 5, 1]]
        pattern_bin = np.zeros(pattern_gray.shape, dtype=bool)

        for ii in range(ny):
            for jj in range(nx):
                pattern_bin[ii, jj] = np.round(pattern_gray[ii, jj])
                err = pattern_gray[ii, jj] - pattern_bin[ii, jj]

                if jj < (nx - 1):
                    pattern_gray[ii, jj+1] += err * 7/16

                if ii < (ny - 1):
                    if jj > 0:
                        pattern_gray[ii + 1, jj - 1] += err * 3/16
                    pattern_gray[ii + 1, jj] += err * 5/16
                    if jj < (ny - 1):
                        pattern_gray[ii + 1, jj + 1] += err * 1/16
    elif mode == "jjn":
        # error diffusion Kernel =
        # 1/48 * [[_, _, #, 7, 5], [3, 5, 7, 5, 3], [1, 3, 5, 3, 1]]
        pattern_bin = np.zeros(pattern_gray.shape, dtype=bool)

        for ii in range(ny):
            for jj in range(nx):
                pattern_bin[ii, jj] = np.round(pattern_gray[ii, jj])
                err = pattern_gray[ii, jj] - pattern_bin[ii, jj]

                if jj < (nx - 1):
                    pattern_gray[ii, jj + 1] += err * 7/48
                if jj < (nx - 2):
                    pattern_gray[ii, jj + 2] += err * 5/48

                if ii < (ny - 1):
                    if jj > 1:
                        pattern_gray[ii + 1, jj - 2] += err * 3/48

                    if jj > 0:
                        pattern_gray[ii + 1, jj - 1] += err * 5/48

                    pattern_gray[ii + 1, jj] += err * 7/48

                    if jj < (ny - 1):
                        pattern_gray[ii + 1, jj + 1] += err * 5/48

                    if jj < (ny - 2):
                        pattern_gray[ii + 1, jj + 2] += err * 3/48

            if ii < (ny - 2):
                if jj > 1:
                    pattern_gray[ii + 2, jj - 2] += err * 1/48

                if jj > 0:
                    pattern_gray[ii + 2, jj - 1] += err * 3/48

                pattern_gray[ii + 2, jj] += err * 5/48

                if jj < (ny - 1):
                    pattern_gray[ii + 2, jj + 1] += err * 3/48

                if jj < (ny - 2):
                    pattern_gray[ii + 2, jj + 2] += err * 1/48

    elif mode == "random":
        pattern_bin = np.asarray(np.random.binomial(1, pattern_gray), dtype=bool)
    elif mode == "round":
        pattern_bin = np.asarray(np.round(pattern_gray), dtype=bool)
    else:
        raise ValueError("mode must be 'floyd-steinberg', 'random', or 'round' but was '%s'" % mode)

    return pattern_bin


# utility functions
def min_angle_diff(angle1,
                   angle2,
                   mode='normal'):
    """
    Find minimum magnitude of angular difference between two angles.

    :param float or np.array angle1: in radians
    :param float or np.array angle2: in radians
    :param str mode: "normal" or "half"

    :return np.array angle_diff:
    """

    # take difference modulo 2pi, which gives positive distance
    angle_diff = np.asarray(np.mod(angle1 - angle2, 2*np.pi))

    # still want smallest magnitude difference (negative or positive). If larger than pi, can express as smaller
    # magnitude negative distance
    ind_greater_pi = angle_diff > np.pi
    angle_diff[ind_greater_pi] = angle_diff[ind_greater_pi] - 2 * np.pi

    if mode == 'normal':
        pass
    elif mode == 'half':
        # compute differences allowing theta and theta + pi to be equivalent
        angle_diff_pi = min_angle_diff(angle1, angle2 + np.pi, mode='normal')
        to_switch = np.abs(angle_diff_pi) < np.abs(angle_diff)
        angle_diff[to_switch] = angle_diff_pi[to_switch]

    else:
        raise ValueError("'mode' must be 'normal' or 'half', but was '%s'" % mode)

    return angle_diff


# generate single pattern
def find_closest_pattern(period: float,
                         angle: float,
                         nphases: int = 1,
                         avec_max_size: int = 40,
                         bvec_max_size: int = 40):
    """
    Find pattern vectors for pattern with an approximate period and angle that also satisfies the perfect phase
    shift condition

    :param period:
    :param angle:
    :param nphases:
    :param avec_max_size:
    :param bvec_max_size:

    :return avec:
    :return bvec:
    :return period_real:
    :return angle_real:
    """

    angles_proposed, bvecs_proposed = find_allowed_angles(period, nphases, bvec_max_size,
                                                          restrict_to_coordinate_axes=False)
    ia = np.argmin(np.abs(angle - angles_proposed))
    a = angles_proposed[ia]
    bvec = bvecs_proposed[ia]

    # approximate a-vector
    x, y, seq = find_rational_approx_angle(a, avec_max_size)
    avec = np.array([x, y])

    period_real = get_sim_period(avec, bvec)
    angle_real = get_sim_angle(avec, bvec)

    return avec, bvec, period_real, angle_real


# tools for finding nearest SIM pattern set
def find_closest_multicolor_set(period: float,
                                nangles: int,
                                nphases: int,
                                wavelengths: list[float] = None,
                                bvec_max_size: int = 40,
                                avec_max_size: int = 40,
                                atol: float = np.pi/180,
                                ptol_relative: float = 0.1,
                                angle_sep_tol: float = 5*np.pi/180,
                                max_solutions_to_search: int = 20,
                                pitch: float = 7560.,
                                minimize_leakage: bool = True):
    """
    Generate set of SIM patterns for multiple colors with period close to specified value and maximizing distance
     between angles. The patterns are determined such that the diffracted orders will pass through the same positions
     in the Fourier plane of the imaging sytem. i.e. the fractional resolution increase in SIM should be the same
     for all of the colors.

     NOTE: for achieving multicolor SIM with a DMD there is more to the story --- you must first find
     an input and output angle which match the diffraction output angles and satisfy the Blaze condition
     for both colors, which is no easy feat!

    :param float period: pattern period in mirrors. If using multiple colors, specify this for the shortest wavelength
    :param int nangles: number of angles
    :param int nphases: number of phases
    :param list wavelengths: list of wavelengths in consistent units. If set to None, then will assume only
     one wavelength.
    :param int bvec_max_size: maximum allowed size of b-vectors, in mirrors
    :param int avec_max_size: maximum allowed size of a-vectors, in mirrors
    :param float atol: maximum allowed deviation between angles for different colors.
    :param float ptol_relative: maximum tolerance for period deviations, as a fraction of the period
    :param float angle_sep_tol: maximum deviation between adjacent pattern angles from the desired value which
    would lead to equally spaced patterns.
    :param int max_solutions_to_search: maximum number of angle combinations to search for furthest
    distance to leakage peaks
    :param float pitch: DMD micromirror spacing in the same units as wavelength
    :param bool minimize_leakage: whether or not to do leakage minimization

    :return vec_as:
    :return vec_bs:
    :return periods_out:
    :return angles_out:
    :return min_leakage_angle:
    """

    # todo: still problems with even number phase shifts

    if wavelengths is None:
        wavelengths = [1]

    # factor to multiply the period by for each wavelength
    factors = np.sort(wavelengths / np.min(wavelengths))
    periods = period * factors

    # get allowed angles in range [0, pi] for all wavelengths
    angles_all = []
    bvs_all = []
    for p in periods:
        a, b = find_allowed_angles(p, nphases, bvec_max_size, restrict_to_coordinate_axes=False)
        angles_all.append(a)
        bvs_all.append(b)

    # only keep angles that are very similar
    # todo: could in principle keep increasing bvec_max_size until have enough angles to work with
    angles_kept = [[] for _ in wavelengths]
    bvs_kept = [[] for _ in wavelengths]

    # todo: could check difference accounting for e.g. 0, pi being same. Probably these edge cases not very important.
    # todo: could think about dynamically changing size of max_bvecs and max_avecs until have an appropriate size.
    for a in angles_all[0]:
        keep = True
        for angs in angles_all:
            if np.min(np.abs(a - angs)) > atol:
                keep = False
                break

        if keep:
            for ii, angs in enumerate(angles_all):
                ind = np.argmin(np.abs(a - angs))
                angles_kept[ii].append(angs[ind])
                bvs_kept[ii].append(bvs_all[ii][ind])

    # do typical minimization using one set of these angles. i.e. "one-color" minimization
    # todo: Now want to do minimization over bvec size, so want to include all colors.
    angles = np.asarray(angles_kept[0])

    expected_angle_sep = np.pi / nangles
    min_sep = expected_angle_sep - angle_sep_tol
    max_sep = expected_angle_sep + angle_sep_tol
    angle_inds = np.asarray(range(len(angles)))
    # for each angle, find the allowed successor angles
    successor_inds = [angle_inds[np.logical_and(angles > a + min_sep, angles < a + max_sep)] for a in angles]

    # list of lists, with each sublist giving possible set of angles
    # can grow these sets by looking only at the last angle and its possible successors
    sets_inds = [[ind] for ind in angle_inds]
    for ii in range(1, nangles):
        sets_inds_new = []
        for set_current in sets_inds:
            for successor_ind in successor_inds[set_current[-1]]:
                sets_inds_new.append(set_current + [successor_ind])

        sets_inds = sets_inds_new

    # arrays of angles and indices
    sets_inds = np.array(sets_inds)
    angle_sets = angles[sets_inds]

    # get rid of any where separation between n-1 and 0th is too large
    too_big = np.abs(min_angle_diff(angle_sets[:, 0], angle_sets[:, nangles - 1], mode='half') - expected_angle_sep) > angle_sep_tol

    # cost on bvector norms
    bvs_norms = np.array([np.linalg.norm(bv) for bv in bvs_kept[0]])
    cost = np.sum(bvs_norms[sets_inds] / nphases, axis=1)

    cost[too_big] = np.nan

    # sort choices by cost
    # isort = np.flip(np.argsort(cost.ravel()))
    isort = np.argsort(cost.ravel())
    sets_inds_sort = sets_inds[isort]
    csort = cost.ravel()[isort]

    # isort = isort[np.logical_not(np.isnan(csort))]
    sets_inds_sort = sets_inds_sort[np.logical_not(np.isnan(csort))]
    # csort = csort[np.logical_not(np.isnan(csort))]

    if not minimize_leakage:
        # take closest solution
        sopt = sets_inds_sort[0]
        vec_bs = [[bvs_wvl[s] for s in sopt] for bvs_wvl in bvs_kept]
        angles_opt = [np.asarray([angs_wvl[s] for s in sopt]) for angs_wvl in angles_kept]
        vec_as = [[find_rational_approx_angle(a, avec_max_size)[-1][-1] for a in a_wavlen] for a_wavlen in angles_opt]
        min_leakage_angle = np.nan
    else:
        # loop over so many possible solutions and check which has most leeway wrt leakage orders
        # todo: should ensure that all the angle sets looped over are close enough to the optimum
        min_leakage_angle = 0

        for sopt in sets_inds_sort[:max_solutions_to_search]:

            # list of lists. List holds lists of and b-vectors for each wavelength
            angles_opt = [np.asarray([angs_wvl[s] for s in sopt]) for angs_wvl in angles_kept]
            vec_bs_proposed = [[bvs_wvl[s] for s in sopt] for bvs_wvl in bvs_kept]

            # find vec_as satisfying approximate angle
            vec_as_proposed = [[] for _ in wavelengths]
            min_leakage_dist_wvlen = [[] for _ in wavelengths]
            for ii in range(len(wavelengths)):

                vec_as_accepted = [[] for _ in angles_opt[ii]]
                for jj, (a, vb) in enumerate(zip(angles_opt[ii], vec_bs_proposed[ii])):
                    xsh, ysh, vec_a_seq = find_rational_approx_angle(a, avec_max_size)
                    vec_as_accepted[jj] = [va for va in vec_a_seq
                                           if np.cross(va, vb) != 0 and
                                           min_angle_diff(get_sim_angle(va, vb), a, mode='half') < atol and
                                           np.abs((get_sim_period(va, vb) - periods[ii]) / periods[ii]) < ptol_relative]

                # #######################################
                # find set of vec_as with maximum distance to nearest leakage orders
                # #######################################
                vava = np.meshgrid(*[range(len(v)) for v in vec_as_accepted], indexing='ij')
                min_dists = np.zeros(vava[0].shape)

                for kk in range(vava[0].size):
                    ind_prop = np.unravel_index(kk, vava[0].shape)
                    vec_as_curr = [vec_as_accepted[ll][vava[ll][ind_prop]] for ll in range(nangles)]
                    # min_dists[ind_prop], _, _ = find_nearest_leakage_peaks(vec_as_curr, vec_bs_proposed[ii], nphases)
                    min_dists[ind_prop], _, _ = find_nearest_leakage_peaks(vec_as_curr, vec_bs_proposed[ii], nphases,
                                                                           wavelength=wavelengths[ii], pitch=pitch)

                ind_min = np.argmax(min_dists)
                sub_min = np.unravel_index(ind_min, min_dists.shape)

                # multiple by wavelength factor to account for the fact the scale of the Fourier plane
                # changes with wavelength
                # min_leakage_dist_wvlen[ii] = factors[ii] * min_dists[sub_min]
                min_leakage_dist_wvlen[ii] = min_dists[sub_min]
                vec_as_proposed[ii] = [vec_as_accepted[ll][vava[ll][sub_min]] for ll in range(nangles)]

            # accept new set of angles if closest leakage order is further than what we already have
            proposed_min_leakage_dist = np.min(min_leakage_dist_wvlen)
            if proposed_min_leakage_dist > min_leakage_angle:
                min_leakage_angle = proposed_min_leakage_dist
                vec_as = vec_as_proposed
                vec_bs = vec_bs_proposed

    return np.array(vec_as), np.array(vec_bs)


def find_allowed_angles(period: float,
                        nphases: int,
                        nmax: int,
                        restrict_to_coordinate_axes: bool = False):
    """
     Given a DMD pattern with fixed period of absolute value P, get allowed pattern angles in the range [0, pi] for
     which the pattern allows perfect phase shifting for nphases.

     P = dxb * cos(theta) + dyb * sin(theta)

     For theta in [0, pi] we can take x=cos(theta), and sin(theta) = sqrt(1-x^2). We get a quadratic equation in x,
     x^2 * (dxb**2/dyb**2 + 1) - x * (2*P*dxb/dyb**2) + (P**2/dxb**2 - 1) = 0

    :param period:
    :param nphases:
    :param nmax:
    :param restrict_to_coordinate_axes: deprecated...used to allow running old behavior when adding functionatlity
    :return:
    """

    # allowed vector components
    if restrict_to_coordinate_axes:
        ns = np.arange(nphases, nmax, nphases)
        dxb = np.concatenate((ns, np.zeros(ns.shape)))
        dyb = np.concatenate((np.zeros(ns.shape), ns))
    else:
        # with two vector components, can no longer restrict all to be positive
        dxs = np.arange(nphases, nmax, nphases, dtype=float)
        dxs = np.concatenate((np.flip(-dxs), np.array([0]), dxs), axis=0)

        dys = np.arange(0, nmax, nphases, dtype=float)

        dxb, dyb = np.meshgrid(dxs, dys)
    # exclude vb = [0, 0]
    dxb, dyb = dxb[dxb**2 + dyb**2 > 0], dyb[dxb**2 + dyb**2 > 0]

    # (1) P = dxb * cos(theta) + dyb * sin(theta)
    # (2) P = dxb * x + dyb * sqrt(1-x**2)
    # squaring both sides gives
    # (3) (P - dxb * x)**2 = dyb**2 * (1 - x**2)
    # A*x**2 + B*x + C = 0
    # two solutions, expect one in [0, pi/2] and one in [pi/2, pi].
    # BUT it is possible these are not both solutions to the original equation. This can happen if the portion in
    # paranetheses on the LHS of (3) is negative
    A = dxb**2 + dyb**2
    B = - 2 * period * dxb
    C = period**2 - dyb**2

    with np.errstate(invalid='ignore'):
        # get solutions to the squared problem
        x1 = 0.5 * (-B + np.sqrt(B**2 - 4 * A * C)) / A
        # only keep ones that also satisfy the base problem
        x1[np.abs(dxb * x1 + dyb * np.sqrt(1 - x1**2) - period) > 1e-7] = np.nan
        x2 = 0.5 * (-B - np.sqrt(B**2 - 4 * A * C)) / A
        x2[np.abs(dxb * x2 + dyb * np.sqrt(1 - x2 ** 2) - period) > 1e-7] = np.nan
        # also negative period solutions. Only change here is B -> -B
        x3 = 0.5 * (B + np.sqrt(B**2 - 4 * A * C)) / A
        x3[np.abs(dxb * x3 + dyb * np.sqrt(1 - x3 ** 2) + period) > 1e-7] = np.nan
        x4 = 0.5 * (B - np.sqrt(B**2 - 4 * A * C)) / A
        x4[np.abs(dxb * x4 + dyb * np.sqrt(1 - x4 ** 2) + period) > 1e-7] = np.nan

        # get final angles and vectors
        angles = np.concatenate((np.arccos(x1).ravel(), np.arccos(x2).ravel(),
                                 np.arccos(x3).ravel(), np.arccos(x4).ravel()))

    # exclude nans
    vbs = 4 * [[int(dx), int(dy)] for dx, dy in zip(dxb.ravel(), dyb.ravel())]
    # test
    # dxbt, dybt = zip(*vbs)
    # ps = np.cos(angles) * np.asarray(dxbt) + np.sin(angles) * np.asarray(dybt)
    vbs = [v for v, a in zip(vbs, angles) if not np.isnan(a)]
    angles = angles[np.logical_not(np.isnan(angles))]

    # sort lists by size of angles
    isort = np.argsort(angles)
    angles = angles[isort]
    vbs = [vbs[ii] for ii in isort]

    return angles, vbs


def find_rational_approx_angle(angle: float,
                               nmax: int):
    """
    Find closest allowed a-vector for a given angle and maximum number of mirrors

    :param angle: desired angle in radians
    :param nmax: maximum size of the x- and y-components of the a-vector, in mirrors.

    :return xshift:
    :return yshift:
    :return vecs:
    """

    # todo: how to simplify these cases
    # first convert angle to [0, pi/2], so can do rational approximation for positive fraction
    angle_2p = np.mod(angle, 2*np.pi)
    if angle_2p <= np.pi/2:
        angle_pos = angle_2p
        case = 1
    elif angle_2p > np.pi/2 and angle_2p <= np.pi:
        angle_pos = np.pi - angle_2p
        case = 2
    elif angle_2p > np.pi and angle_2p <= 3*np.pi/2:
        angle_pos = angle_2p - np.pi
        case = 3
    elif angle_2p > 3*np.pi/2 and angle_2p <= 2*np.pi:
        angle_pos = 2*np.pi - angle_2p
        case = 4
    else:
        raise ValueError('disallowed angle')

    slope = np.tan(angle_pos)
    slope_inverted = False
    if slope > 1:
        slope = 1 / slope
        slope_inverted = True

    # use Farey sequence and binary search. See e.g.
    # https://www.johndcook.com/blog/2010/10/20/best-rational-approximation/
    fr_lb = [0, 1]
    fr_ub = [1, 1]
    approximate_seq = []
    while True:
        mediant_num = fr_lb[0] + fr_ub[0]
        mediant_denom = fr_lb[1] + fr_ub[1]
        if mediant_denom >= nmax:
            break

        mediant = mediant_num / mediant_denom
        if mediant == slope:
            fr_ub = [mediant_num, mediant_denom]
            fr_lb = [mediant_num, mediant_denom]
            approximate_seq.append(fr_ub)

        if mediant > slope:
            fr_ub = [mediant_num, mediant_denom]

            # compare new bound to last best estimate
            if approximate_seq == []:
                approximate_seq.append(fr_ub)
            else:
                current_est = mediant_num/mediant_denom
                best_est = approximate_seq[-1][0] / approximate_seq[-1][1]
                if np.abs(current_est - slope) <= np.abs(best_est - slope):
                    approximate_seq.append(fr_ub)

        else:
            fr_lb = [mediant_num, mediant_denom]

            # compare new bound to last best estimate
            if approximate_seq == []:
                approximate_seq.append(fr_lb)
            else:
                current_est = mediant_num / mediant_denom
                best_est = approximate_seq[-1][0] / approximate_seq[-1][1]
                if np.abs(current_est - slope) <= np.abs(best_est - slope):
                    approximate_seq.append(fr_lb)

    if slope_inverted:
        slope = 1 / slope
        approximate_seq = [np.flip(s) for s in approximate_seq]

    # todo: don't really understand why each sign needs to be so. Thought I had it figured out, but found I had to
    # change all except case 1
    # tan(theta) = (-dxa) / dya
    if case == 1:
        vecs = [[-s[0], s[1]] for s in approximate_seq]
    elif case == 2:
        vecs = [[-s[0], -s[1]] for s in approximate_seq]
    elif case == 3:
        vecs = [[s[0], -s[1]] for s in approximate_seq]
    elif case == 4:
        vecs = [[s[0], s[1]] for s in approximate_seq]

    xshift = vecs[-1][0]
    yshift = vecs[-1][1]

    return xshift, yshift, vecs


def find_allowed_periods(angle: float,
                         nphases: int,
                         nmax: int):
    """
    Given a DMD pattern with fixed angle, get allowed pattern periods which allow perfect phase shifting for nphases

    Recall that for vec_a = [dxa, dya] and vec_b = [dxb, 0], and dxb = l * nphases for perfect phase shifting
    period = dxb * dya/|vec_a| = dxb * cos(theta)
    theta = angle(vec_a_perp) = arctan(-dxa / dya)
    P = np.cos(theta) * l*nphases

    on the other hand, if vec_b = [0, dyb]
    period = dyb * -dxa/|vec_a = dyb * sin(theta)
    P = np.sin(theta) * l*nphases

    :param angle:
    :param nphases:
    :param nmax:

    :return list[float] periods:
    :return list[int] ls:
    :return list[] is_xlike:
    """
    ls = np.arange(1, int(np.floor(nmax / nphases)))

    p1 = np.cos(angle) * ls * nphases
    p2 = np.sin(angle) * ls * nphases

    # store data about angles
    is_xlike = np.concatenate((np.ones(p1.size), np.zeros(p2.size)), axis=0)
    ls_all = np.concatenate((ls, ls), axis=0)
    periods = np.concatenate((p1, p2), axis=0)

    # sort lists by size of angles
    combined_list = list(zip(periods, ls_all, is_xlike))
    combined_list.sort(key=lambda v: v[0])
    periods, ls_all, is_xlike = zip(*combined_list)

    return np.asarray(periods), np.asarray(ls_all), np.asarray(is_xlike)


def find_nearest_leakage_peaks(vec_as,
                               vec_bs,
                               nphases: int = 3,
                               minimum_relative_peak_size: float = 1e-3,
                               wavelength: float = 1.,
                               pitch: float = 7560.):
    """
    Find minimum distance between main pattern frequency and leakage frequencies from other patterns in the set

    :param list[int] or np.array vec_as: list of a vectors
    :param list[int] or np.array vec_bs: list of b vectors
    :param int nphases:
    :param int minimum_relative_peak_size: peaks smaller than this size (compared with the maximum peak,
     i.e. the DC peak)
    will not be included.
    :param int wavelength: can be provided so that distance will be appropriately scaled for different wavelengths
    :param float pitch:

    :return min_angle_all:
    :return min_angle_leakage_peaks:
    :return leakage_order_pattern_index:
    """

    # find frequencies
    nangles = len(vec_as)

    # frqs = [get_sim_frqs(va, vb) for va, vb in zip(vec_as, vec_bs)]
    cells, xs, ys = zip(*[get_sim_unit_cell(va, vb, nphases) for va, vb in zip(vec_as, vec_bs)])
    xxs, yys = zip(*[np.meshgrid(x, y) for x, y in zip(xs, ys)])
    recp_vects = [get_reciprocal_vects(va, vb) for va, vb in zip(vec_as, vec_bs)]

    min_dists = np.ones((nangles, nangles)) * np.inf
    for ii in range(nangles):
        for jj in range(nangles):
            if ii == jj:
                # allow nearby combinations except for
                ns, ms = np.meshgrid([-1, 0, 1], [-1, 0, 1, 2])
                ns = ns.ravel()
                ms = ms.ravel()

                ns, ms = ns[np.logical_and(ns != 0, ms != 1)], ms[np.logical_and(ns != 0, ms != 1)]
            else:
                # want to find combinations of reciprocal vectors of one pattern that are closest to
                # those of another pattern
                # n * r1 + m * r2 ~ recp_vec_b, with n,m integers
                # first, solve for n, m real numbers
                mat = np.linalg.inv(np.concatenate((recp_vects[jj][0], recp_vects[jj][1]), axis=1))
                n, m = mat.dot(recp_vects[ii][1])

                # now check distances for nearby reciprocal vectors. Expand our search in case some of the nearby peaks
                # have very little weight
                ns, ms = np.meshgrid(list(range(int(np.floor(n)) - 2, int(np.ceil(n)) + 3)),
                                     list(range(int(np.floor(m)) - 2, int(np.ceil(m)) + 3)))
                ns = ns.ravel()
                ms = ms.ravel()

            for n, m in zip(ns, ms):
                vec = n * recp_vects[jj][0] + m * recp_vects[jj][1]

                # peak weight is the Fourier transform over the unit cell (divided by the DC component)
                weight = np.abs(np.nansum(cells[jj] * np.exp(1j * 2 * np.pi * (vec[0] * xxs[jj] + vec[1] * yys[jj]))) / np.nansum(cells[jj]))
                # if weight is too small, don't count distance
                if weight < minimum_relative_peak_size:
                    continue

                # if weight is not too small, then set distance if it is smaller than what we already have
                dist = np.linalg.norm(vec - recp_vects[ii][1]) * wavelength / pitch
                if dist < min_dists[ii, jj]:
                    min_dists[ii, jj] = dist

    # minimum distance for each pattern
    min_angle_leakage_peaks = np.nanmin(min_dists, axis=1)
    leakage_order_pattern_index = np.nanargmin(min_dists, axis=1)

    # minimum distance over patterns
    min_angle_all = np.min(min_angle_leakage_peaks)

    return min_angle_all, min_angle_leakage_peaks, leakage_order_pattern_index


# functions for obtaining and exporting results
def vects2pattern_data(dmd_size: list[int],
                       vec_as,
                       vec_bs,
                       nphases: int = 3,
                       wavelength: float = None,
                       invert: bool = False,
                       pitch: float = 7560,
                       generate_patterns: bool = True):
    """
    Generate pattern and useful data (angles, phases, frequencies, reciprocal vectors, ...) from the lattice
    vectors for a given pattern set.

    :param dmd_size: [nx, ny]
    :param np.array vec_as: NumPy array, size nangles x nphases x 2
    :param np.array vec_bs:
    :param nphases:
    :param wavelength: wavelength in nm
    :param invert: whether or not pattern is "inverted", i.e. if the roll of "OFF" and "ON" should be flipped
    :param pitch: DMD micromirror pitch
    :param generate_patterns:

    :return patterns, vec_as, vec_bs, angles, frqs, periods, phases, recp_vects_a, recp_vects_b, min_leakage_angle:
    """

    vec_as = np.array(vec_as, copy=True)
    vec_bs = np.array(vec_bs, copy=True)

    # extract dmd size
    nx, ny = dmd_size

    if wavelength is None:
        wavelength = 1

    nangles, _ = vec_as.shape

    patterns = np.zeros((nangles, nphases, ny, nx))
    phases = np.zeros((nangles, nphases))

    angles = np.zeros(nangles)
    periods = np.zeros(nangles)
    frqs = np.zeros((nangles, 2))
    recp_vects_a = np.zeros((nangles, 2))
    recp_vects_b = np.zeros((nangles, 2))

    # loop over wavelengths
    min_leakage_angle, _, _ = find_nearest_leakage_peaks(vec_as, vec_bs, nphases,
                                                         minimum_relative_peak_size=1e-3,
                                                         wavelength=wavelength, pitch=pitch)

    # loop over angles and find closest available patterns
    for ii in range(nangles):
        ra, rb = get_reciprocal_vects(vec_as[ii], vec_bs[ii])
        recp_vects_a[ii] = ra[:, 0]
        recp_vects_b[ii] = rb[:, 0]

        periods[ii] = get_sim_period(vec_as[ii], vec_bs[ii])
        angles[ii] = get_sim_angle(vec_as[ii], vec_bs[ii])
        frqs[ii] = get_sim_frqs(vec_as[ii], vec_bs[ii])

        for jj in range(nphases):
            phases[ii, jj] = get_sim_phase(vec_as[ii], vec_bs[ii], nphases, jj, dmd_size)

            if generate_patterns:
                patterns[ii, jj], c = get_sim_pattern([nx, ny], vec_as[ii], vec_bs[ii], nphases, jj)

    if invert:
        patterns = 1 - patterns

    return patterns, vec_as, vec_bs, angles, frqs, periods, phases, recp_vects_a, recp_vects_b, min_leakage_angle


def plot_sim_pattern_sets(patterns,
                          vas,
                          vbs,
                          wavelength: float = None,
                          pitch: float = 7560.,
                          figsize=(16, 12),
                          **kwargs):
    """
    Plot all angles/phases in pattern set, as well as their Fourier transforms

    :param patterns:
    :param vas:
    :param vbs:
    :param wavelength:
    :param pitch:
    :param figsize:
    :param kwargs: passed through to figure

    :return figh: handle to resulting figure
    """

    nangles, nphases, ny, nx = patterns.shape

    _, vas, vbs, angles, frqs, periods, phases, recp_vects_a, recp_vects_b, min_leakage_angle = \
        vects2pattern_data([nx, ny], vas, vbs, nphases=nphases, wavelength=wavelength,
                           generate_patterns=False, pitch=pitch)

    # display summary of patterns
    figh = plt.figure(figsize=figsize, **kwargs)
    grid = figh.add_gridspec(nrows=nphases + 1, ncols=nangles)

    if wavelength is not None:
        figh.suptitle(f"sim pattern diagnostic, wavelength = {wavelength:.0f}nm,"
                     f" min leakage angle={min_leakage_angle * 180/np.pi:.3f}deg")
    else:
        figh.suptitle(f"sim pattern diagnostic, min leakage angle = {min_leakage_angle:.3f}")

    # ###############################
    # real space patterns
    # ###############################
    for ii in range(nangles):
        for jj in range(nphases):
            ax = figh.add_subplot(grid[jj, ii])
            # cut_size = int(np.max(np.abs(vas)) * np.ceil(period / np.max(np.abs(vbs))))
            cut_size = int(np.max([np.max(np.abs(vas)), np.max(np.abs(vbs))]))

            ax.imshow(patterns[ii, jj, :cut_size, :cut_size], cmap="bone")
            ax.set_ylabel(f"phase={phases[ii, jj] * 180 / np.pi:.2f}deg")
            if jj == 0:
                ax.set_title(f"angle={angles[ii] * 180 / np.pi:.2f}deg,"
                             f" p={periods[ii]:.2f}\n"
                             f"a = [{vas[ii, 0]:d}, {vas[ii, 1]:d}],"
                             f" b=[{vbs[ii, 0]:d}, {vbs[ii, 1]:d}]")
            else:
                ax.set_xticklabels([])
                ax.set_yticklabels([])

    # ###############################
    # Fourier transforms
    # ###############################
    fx = fft.fftshift(fft.fftfreq(nx, 1))
    dfx = fx[1] - fx[0]
    fy = fft.fftshift(fft.fftfreq(ny, 1))
    dfy = fy[1] - fy[0]
    df_min = np.min([fx[1] - fx[0], fy[1] - fy[0]])
    extent = [fx[0] - 0.5 * dfx, fx[-1] + 0.5 * dfx,
              fy[-1] + 0.5 * dfy, fy[0] - 0.5 * dfy]

    for ii in range(nangles):
        ax = figh.add_subplot(grid[nphases, ii])
        # 2D window from broadcasting
        apodization = np.expand_dims(scipy.signal.windows.hann(nx), axis=0) * \
                 np.expand_dims(scipy.signal.windows.hann(ny), axis=1)

        ft = fft.fftshift(fft.fft2(fft.ifftshift(patterns[ii, 0] * apodization)))
        ax.imshow(np.abs(ft) / np.abs(ft).max(), norm=PowerNorm(gamma=0.1), extent=extent, cmap="bone")

        # dominant frequencies of underlying patterns
        for rr in range(nangles):
            if rr == ii:
                color = 'r'
            else:
                color = 'm'
            ax.add_artist(Circle((frqs[rr, 0], frqs[rr, 1]), radius=5 * df_min, color=color, fill=0, ls='-'))
            ax.add_artist(Circle((-frqs[rr, 0], -frqs[rr, 1]), radius=5 * df_min, color=color, fill=0, ls='-'))

        ax.set_ylabel('ft')

    return figh


def export_pattern_set(dmd_size: list[int],
                       vec_as,
                       vec_bs,
                       nphases: int = 3,
                       invert: bool = False,
                       pitch: float = 7560.,
                       wavelength: float = 1.,
                       save_dir='sim_patterns',
                       plot_results: bool = False):
    """
    Export a single set of SIM patterns, i.e. single wavelength, single period

    :param dmd_size: [nx, ny]
    :param np.array vec_as: nangles x nphases x 2
    :param np.array vec_bs:
    :param nphases:
    :param invert:
    :param pitch:
    :param wavelength:
    :param save_dir:
    :param plot_results:

    :return patterns, data, figh:
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(exist_ok=True)

    patterns, vec_as, vec_bs, angles, frqs, periods, phases, recp_vects_a, recp_vects_b, min_leakage_angle = \
        vects2pattern_data(dmd_size, vec_as, vec_bs, nphases=nphases, wavelength=None, invert=invert, pitch=pitch)

    nangles, _, ny, nx = patterns.shape

    # save data
    data = {'vec_as': vec_as.tolist(),
            'vec_bs': vec_bs.tolist(),
            'frqs': frqs.tolist(),
            'angles': angles.tolist(),
            'periods': periods.tolist(),
            'phases': phases.tolist(),
            'nx': int(dmd_size[0]),
            'ny': int(dmd_size[1]),
            'recp_vects_a': recp_vects_a.tolist(),
            'recp_vects_b': recp_vects_b.tolist(),
            'min_leakage_angle': float(min_leakage_angle),
            'dmd_pitch': float(pitch),
            'wavelength': float(wavelength)}

    fpath = save_dir / f"sim_patterns_period={np.mean(periods):.2f}_nangles={nangles:d}.json"
    with open(fpath, 'w') as f:
        json.dump(data, f, indent="\t")

    # save patterns to separate PNG files
    for ii in range(nangles):
        for jj in range(nphases):
            ind = ii * nphases + jj
            # save file
            # need to convert so not float to save as PNG
            im = Image.fromarray(patterns[ii, jj].astype('bool'))
            im.save(save_dir / f"{ind:02d}_period={periods[ii]:.2f}_angle={angles[ii] * 180/np.pi:.1f}deg_phase={phases[ii, jj]:.2f}.png")

    # save patterns in tif stack
    fpath = save_dir / f"sim_patterns_period={np.mean(periods):.2f}_nangles={nangles:d}_nphases={nphases:d}.tif"
    tifffile.imwrite(fpath,
                     tifffile.transpose_axes(patterns.astype(np.uint8).reshape((nangles * nphases, ny, nx)), "CYX", asaxes="TZQCYXS"),
                     imagej=True)
    # im_list = [Image.fromarray(patterns[ii, jj].astype('bool')) for ii in range(nangles) for jj in range(nphases)]
    # im_list[0].save(fpath, save_all=True, append_images=im_list[1:])

    if plot_results:
        figh = plot_sim_pattern_sets(patterns, vec_as, vec_bs, wavelength)
        figh.savefig(save_dir / f"period={np.mean(periods):.2f}_pattern_summary.png")
    else:
        figh = None

    return patterns, data, figh


# main function for generating SIM patterns at several frequencies and wavelengths
def export_all_pattern_sets(dmd_size: list[int],
                            periods: list[float],
                            nangles: int = 3,
                            nphases: int = 3,
                            wavelengths: list[float]=None,
                            invert: list[bool] = False,
                            pitch: float = 7560.,
                            save_dir='sim_patterns',
                            plot_results: bool = True,
                            **kwargs):
    """
    Generate SIM pattern sets and save results

    :param list[int] dmd_size: [nx, ny]
    :param list[float] periods: list of approximate periods
    :param int nangles: number of angles
    :param int nphases: number of phases
    :param list[float] or None wavelengths: list of wavelengths in nanometers. If set to None,
     will assume only one wavelength.
    :param list[bool] or bool invert:
    :param float pitch:
    :param str save_dir: directory to save results
    :param bool plot_results:
    :param kwargs: arguments passed through to find_closest_multicolor_set(). Use them to set the
    angle/period tolerances and search range for that function.

    :return data_all: [[dict_period0_wlen0, dict_period0, wlen1, ...], [dict_period1, wlen0, ...]]
    list of list of dictionary objects. First level sublists are data for different periods, second sublevel is data
    for different wavelengths.
    """

    if wavelengths is None:
        wavelengths = [1]

    if not isinstance(wavelengths, list):
        wavelengths = [wavelengths]

    nwavelengths = len(wavelengths)

    if not isinstance(periods, list):
        periods = [periods]

    save_dir = Path(save_dir)
    save_dir.mkdir(exist_ok=True)

    data_all = []

    # generate sets
    for period in periods:
        data_period = []

        # directory to save results
        sub_dir = f"period={period:.1f}_nangles={nangles:d}"
        pattern_save_dir = Path(save_dir, sub_dir)
        # pattern_save_dir = mm_io.get_unique_name(fpath, mode='dir')
        if not pattern_save_dir.exists():
            pattern_save_dir.mkdir()

        vec_as, vec_bs = find_closest_multicolor_set(period, nangles, nphases, wavelengths=wavelengths, **kwargs)

        # loop over wavelengths
        for kk in range(nwavelengths):
            if nwavelengths == 1:
                wavlen_savedir = pattern_save_dir
            else:
                wavlen_savedir = pattern_save_dir / f"wavelength={wavelengths[kk]:d}nm"

            patterns, data, figh = export_pattern_set(dmd_size, vec_as[kk], vec_bs[kk], nphases=nphases,
                                                      wavelength=wavelengths[kk],
                                                      invert=invert[kk], save_dir=wavlen_savedir,
                                                      pitch=pitch, plot_results=plot_results)
            data_period.append(data)

        data_all.append(data_period)

    return data_all


# export calibration patterns
def aberration_map_pattern(dmd_size: list[int],
                           vec_a,
                           vec_b,
                           nphases: int, centers,
                           radius=20,
                           phase_indices: int = 0):
    """
    Generate patterns to calibrate DMD aberrations using the approach of https://doi.org/10.1364/OE.24.013881

    Each pattern contains two small patches of lattice. If we measure the interference of the beams diffracted from
    the two patches, we can extract the surface profile of the DMD.

    :param dmd_size: (nx, ny)
    :param vec_a:
    :param vec_b:
    :param nphases: number of phase shifts allowed
    :param centers:
    :param radius: radius, must be an integer
    :param phase_indices:
    :return:
    """
    if not isinstance(radius, int):
        raise ValueError("radius must be an integer")

    centers = np.array(centers, dtype=int)
    if centers.ndim == 1:
        centers = np.expand_dims(centers, axis=0)

    phase_indices = np.atleast_1d(phase_indices)
    if len(phase_indices) == 1 and centers.shape[0] > 1:
        phase_indices = np.ones(centers.shape[0]) * phase_indices[0]

    # get patches
    pattern_patches = []
    for ii in range(nphases):
        pattern_patch, _ = get_sim_pattern([2 * radius + 1, 2 * radius + 1], vec_a, vec_b, nphases, ii)
        pattern_patches.append(pattern_patch)
    pattern_patches = np.asarray(pattern_patches)

    xx, yy = np.meshgrid(range(pattern_patches.shape[2]), range(pattern_patches.shape[1]))
    xx = xx - xx.mean()
    yy = yy - yy.mean()
    pattern_patches[:, np.sqrt(xx**2 + yy**2) > radius] = 0

    # get pattern
    nx, ny = dmd_size
    pattern = np.zeros((ny, nx))
    for ii in range(len(centers)):
        pattern[centers[ii, 1] - radius: centers[ii, 1] + radius + 1,
                centers[ii, 0] - radius: centers[ii, 0] + radius + 1] = pattern_patches[phase_indices[ii]]

    return pattern

def checkerboard(dmd_size: list[int],
                 n_on: int,
                 n_off: int = None):
    """
    Create checkerboard pattern

    :param dmd_size: [nx, ny]
    :param n_on:
    :param n_off:

    :return np.array pattern:
    """

    # default is use same number of off and on pixels
    if n_off is None:
        n_off = n_on

    nx, ny = dmd_size

    n_cell = n_on + n_off
    cell = np.zeros((n_cell, n_cell))

    cell[:n_on, :n_on] = 1

    nx_tiles = int(np.ceil(nx / cell.shape[1]))
    ny_tiles = int(np.ceil(ny / cell.shape[0]))

    mask = np.tile(cell, [ny_tiles, nx_tiles])
    mask = mask[0:ny, 0:nx]

    return mask


def export_calibration_patterns(dmd_size: list[int],
                                save_dir='',
                                circle_radii=(1, 2, 3, 4, 5, 10, 25, 50, 100, 200, 300)):
    """
    Produce calibration patterns for the DMD, which are all on, all off, center-circles of several sizes,
    and checkerboard patterns of several sizes
    :param dmd_size: [nx, ny]
    :param save_dir:
    :param circle_radii
    :return:
    """
    save_dir = Path(save_dir)
    save_dir.mkdir(exist_ok=True)

    nx = dmd_size[0]
    ny = dmd_size[1]

    # all mirrors on
    all_on = np.ones((ny, nx))
    im = Image.fromarray(all_on.astype('bool'))
    im.save(save_dir / "on.png")

    # all mirror off
    all_off = np.zeros((ny, nx))
    im = Image.fromarray(all_off.astype('bool'))
    im.save(save_dir / "off.png")

    # circles of different radii centered in the middle of DMD
    xx, yy = np.meshgrid(range(nx), range(ny))
    xc = (nx - 1) / 2
    yc = (ny - 1) / 2
    rr = np.sqrt((xx - xc)**2 + (yy - yc)**2)
    for r in circle_radii:
        mask = np.zeros((ny, nx))
        mask[rr <= r] = 1

        im = Image.fromarray(mask.astype('bool'))
        im.save(save_dir / f"circle_on_r={r:d}.png")

        im = Image.fromarray((1 - mask).astype('bool'))
        im.save(save_dir / f"circle_off_r={r:d}.png")

    # checkerboard patterns with different spacing
    periods = np.concatenate((np.arange(2, 12, 1), np.arange(12, 30, 2), np.arange(30, 200, 10)))
    for p in periods:
        on_pix = int(np.ceil(p / 2))
        mask = checkerboard(dmd_size, on_pix)

        im = Image.fromarray(mask.astype('bool'))
        im.save(save_dir / f"checkerboard_period={p:d}.png")

    # patterns with variable spacing
    periods = range(2, 20, 2)
    for ii, p in enumerate(periods):
        cell = np.zeros((p, nx))
        on_pix = int(np.ceil(p / 2))
        cell[:on_pix, :] = 1
        cell = np.tile(cell, [4, 1])

        if ii == 0:
            mask = cell
        else:
            mask = np.concatenate((mask, cell), axis=0)

    mask = mask[:, :mask.shape[0]]

    mask_block = np.concatenate((mask, np.rot90(mask)), axis=1)
    mask_block2 = np.concatenate((np.rot90(mask), mask), axis=1)

    mask_superblock = np.concatenate((mask_block, mask_block2))

    ny_reps = int(np.ceil(ny / mask_superblock.shape[0]))
    nx_reps = int(np.ceil(nx / mask_superblock.shape[1]))
    mask = np.tile(mask_superblock, [ny_reps, nx_reps])
    mask = mask[0:ny, 0:nx]

    im = Image.fromarray(mask.astype('bool'))
    im.save(save_dir / f"variable_pattern_periods={periods[0]:d}_to_{periods[-1]:d}.png")

    # pattern with three corners
    corner_size = 300
    corner_pattern = np.zeros((ny, nx), dtype=bool)
    corner_pattern[:corner_size, :corner_size] = 1
    corner_pattern[:corner_size, -corner_size:] = 1
    corner_pattern[-corner_size:, :corner_size] = 1

    im = Image.fromarray(corner_pattern)
    im.save(f"three_corners_{corner_size:d}.png")


def get_affine_fit_pattern(dmd_size: list[int],
                           radii: tuple[float] = (1., 1.5, 2.),
                           corner_size: int = 4,
                           point_spacing: int = 61,
                           mark_sep: int = 15):
    """
    Create DMD patterns of a sparse 2D grid of points all with the same radius. This is useful for determining the
    affine transformation between the DMD and the camera

    :param dmd_size: [nx, ny]
    :param radii: list of radii of spots for affine patterns.
     If more than one, more than one pattern will be generated.
    :param corner_size: size of blcosk indicating corners
    :param point_spacing: spacing between points
    :param mark_sep: separation between inversion/flip markers near center

    :return patterns, radii, centers:
    """
    if isinstance(radii, (float, int)):
        radii = [radii]

    nx, ny = dmd_size

    # set spacing between points. Does not necessarily need to divide Nx and Ny
    xc = (point_spacing - 1) / 2
    yc = (point_spacing - 1) / 2

    cxs = np.arange(xc, nx, point_spacing)
    cys = np.arange(yc, ny, point_spacing)

    cxcx, cycy = np.meshgrid(cxs, cys)
    centers = np.concatenate((cxcx[:, :, None], cycy[:, :, None]), axis=2)

    patterns = []
    for r in radii:
        one_pt = np.zeros((point_spacing, point_spacing))
        xx, yy = np.meshgrid(range(one_pt.shape[1]), range(one_pt.shape[0]))
        rr = np.sqrt(np.square(xx - xc) + np.square(yy - yc))
        one_pt[rr < r] = 1

        mask = np.tile(one_pt, [int(np.ceil(ny / one_pt.shape[0])), int(np.ceil(nx / one_pt.shape[1]))])
        mask = mask[:ny, :nx]

        # add corners
        mask[:corner_size, :corner_size] = 1
        mask[:corner_size, -corner_size:] = 1
        mask[-corner_size:, :corner_size] = 1
        mask[-corner_size:, -corner_size:] = 1

        # add various markers to fix orientation

        # two edges
        mask[:1, :] = 1
        mask[:, :1] = 1

        # marks near center
        cx = nx // 2
        cy = ny // 2

        # block displaced along x-axis
        xstart1 = cx - mark_sep
        xend1 = xstart1 + corner_size
        ystart1 = cy - corner_size//2
        yend1 = ystart1 + corner_size
        mask[ystart1:yend1, xstart1:xend1] = 1

        # second block along x-axis
        xstart4 = cx - 2 * mark_sep
        xend4 = xstart4 + corner_size
        ystart4 = ystart1
        yend4 = yend1
        mask[ystart4:yend4, xstart4:xend4] = 1

        # central block
        xstart2 = cx - corner_size//2
        xend2 = xstart2 + corner_size
        ystart2 = cy - mark_sep
        yend2 = ystart2 + corner_size
        mask[ystart2:yend2, xstart2:xend2] = 1

        # block displaced along y-axis
        xstart3 = cx - corner_size//2
        xend3 = xstart3 + corner_size
        ystart3 = cy - corner_size//2
        yend3 = ystart3 + corner_size
        mask[ystart3:yend3, xstart3:xend3] = 1

        patterns.append(mask)

    patterns = np.asarray(patterns)

    return patterns, radii, centers


def export_otf_test_set(dmd_size: list[int],
                        pmin: float = 4.5,
                        pmax: float = 50,
                        nperiods: int = 20,
                        nangles: int = 12,
                        nphases: int = 3,
                        avec_max_size: float = 40,
                        bvec_max_size: float = 40,
                        phase_index: int = 0,
                        save_dir=None):
    """
    Export many patterns at different angles/frequencies to test OTF

    :param dmd_size: [nx, ny]
    :param pmin:
    :param pmax:
    :param nperiods:
    :param nangles:
    :param nphases: used to determine the filling fraction of the patterns that are generated
    :param avec_max_size:
    :param bvec_max_size:
    :param phase_index:
    :param str save_dir:

    :return patterns, vec_as, vec_bs:
    """

    nx, ny = dmd_size
    # equally spaced values in frequency space
    fmin = 1 / pmax
    fmax = 1 / pmin
    frqs = np.linspace(fmin, fmax, nperiods)
    periods = np.flip(1/frqs)

    angles = np.arange(nangles) * np.pi / nangles

    patterns = np.zeros((nperiods, nangles, ny, nx), dtype=bool)
    real_angles = np.zeros((nperiods, nangles))
    real_frqs = np.zeros((nperiods, nangles, 2))
    real_periods = np.zeros((nperiods, nangles))
    real_phases = np.zeros((nperiods, nangles))
    vec_as = np.zeros((nperiods, nangles, 2), dtype=int)
    vec_bs = np.zeros((nperiods, nangles, 2), dtype=int)
    # vec_as = [[[''] for _ in range(nangles)] for _ in range(nperiods)]
    # vec_bs = [[[''] for _ in range(nangles)] for _ in range(nperiods)]

    # find nearest patterns
    tstart = time.perf_counter()
    for ii, p in enumerate(periods):
        for jj, a in enumerate(angles):
            tstart_pattern = time.perf_counter()

            vec_as[ii, jj], vec_bs[ii, jj], real_periods[ii, jj], real_angles[ii, jj] = \
                find_closest_pattern(p, a, nphases=nphases, avec_max_size=avec_max_size, bvec_max_size=bvec_max_size)

            patterns[ii, jj], _ = get_sim_pattern(dmd_size, vec_as[ii, jj], vec_bs[ii, jj], nphases, phase_index)
            real_phases[ii, jj] = get_sim_phase(vec_as[ii, jj], vec_bs[ii, jj], nphases,
                                                phase_index, dmd_size, origin='fft')

            tnow = time.perf_counter()
            print(f"generated pattern {ii * len(angles) + jj + 1:d}/{len(periods) * len(angles):d}"
                  f" in {tnow - tstart_pattern:.2f}s,"
                  f"elapsed time {tnow - tstart:.2f}s",
                  end="\r")
    print("")

    pattern_on = np.ones((ny, nx), dtype=np.uint8)
    pattern_off = np.zeros((ny, nx), dtype=np.uint8)

    # export results
    if save_dir is not None:
        # save_dir = mm_io.get_unique_name(save_dir, mode='dir')
        save_dir = Path(save_dir)
        if not save_dir.exists():
            save_dir.mkdir(parents=True)

        # save pattern info
        fpath = save_dir / "pattern_data.json"
        data = {'vec_as': vec_as.tolist(),
                'vec_bs': vec_bs.tolist(),
                'angles': real_angles.tolist(),
                'periods': real_periods.tolist(),
                'frequencies': real_frqs.tolist(),
                'phases': real_phases.tolist(),
                'nphases': nphases,
                'phase_index': phase_index,
                'units': 'um',
                'notes': 'total number of patterns should be nphases*nangles + 2.'
                                        ' The last two patterns are all ON and all OFF respectively.'}

        # with open(fpath, 'wb') as f:
        #     pickle.dump(data, f)
        with open(fpath, "w") as f:
            json.dump(data, f, indent="\t")

        # save patterns as set of pngs
        for ii in range(nperiods):
            for jj in range(nangles):
                ind = ii * nangles + jj
                fpath = save_dir / f"{ind:03d}_pattern_period={real_periods[ii, jj]:.3f}_angle={real_angles[ii, jj] * 180/np.pi:.2f}deg.png"

                # need to convert so not float to save as PNG
                im = Image.fromarray(patterns[ii, jj].astype('bool'))
                im.save(fpath)

        # save all on
        fpath = save_dir / f"{nperiods * nangles:03d}_pattern_all_on.png"
        im = Image.fromarray(pattern_on.astype('bool'))
        im.save(fpath)

        # save all off
        fpath = save_dir / f"{nperiods * nangles + 1:03d}_pattern_all_off.png"
        im = Image.fromarray(pattern_off.astype('bool'))
        im.save(fpath)

        # save patterns as tif
        fpath = save_dir / "otf_patterns.tif"
        patterns_reshaped = np.reshape(patterns, [patterns.shape[0] * patterns.shape[1],
                                                  patterns.shape[2], patterns.shape[3]])
        patterns_reshaped = np.concatenate((patterns_reshaped,
                                            np.expand_dims(pattern_on, axis=0),
                                            np.expand_dims(pattern_off, axis=0)),
                                           axis=0)
        tifffile.imwrite(fpath, patterns_reshaped.astype(np.uint16))

    return patterns, vec_as, vec_bs, real_angles, real_periods
