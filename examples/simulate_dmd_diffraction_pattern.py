"""
Simulate diffraction from a given pattern displayed on the DMD. Do simulations both in "1D", i.e. considering only
diffraction in the tx=-ty plane, and in "2D", i.e. considering the full output space.

Unlike simulate_multicolor_sim_patterns_1d.py, this file simulates the same input angles for all wavelength considered
"""
import numpy as np
import matplotlib.pyplot as plt
import mcsim.analysis.simulate_dmd as dmd

wavelengths = [465e-9, 532e-9, 635e-9]
colors = ["b", "g", "r"]

tx_ins = -np.array([35]) * np.pi / 180
ty_ins = np.array([30]) * np.pi / 180
_, tm_ins = dmd.angle2pm(tx_ins, ty_ins)

# pixel spacing (DMD pitch)
dx = 7.56e-6  # meters
dy = dx
# pixel size, inferred from coverage fraction
coverage_f = 0.92
wx = np.sqrt(coverage_f) * dx
wy = wx
# on and off angle of mirrors from the normal plane of the DMD body
gamma_on = 12 * np.pi / 180
gamma_off = -12 * np.pi / 180
rot_axis = (1/np.sqrt(2), 1/np.sqrt(2), 0)

# create pattern
nx = 50
ny = 50

# set pattern for DMD to be simulated
[xx, yy] = np.meshgrid(range(nx), range(ny))
theta_pattern = -np.pi / 4
period = 3 * np.sqrt(2)

pattern = np.cos(2 * np.pi / period * (xx * np.cos(theta_pattern) + yy * np.sin(theta_pattern)))
pattern[pattern <= 0] = 0
pattern[pattern > 0] = 1
# pattern[pattern == 0] = 1

# sample 1D simulation
data1d = dmd.simulate_1d(pattern,
                         wavelengths,
                         gamma_on,
                         rot_axis,
                         gamma_off,
                         rot_axis,
                         dx,
                         dy,
                         wx,
                         wy,
                         tm_ins)
dmd.plot_1d_sim(data1d, colors, save_dir=None)

# sample 2D simulation
data2d = dmd.simulate_2d(pattern,
                         wavelengths,
                         gamma_on,
                         rot_axis,
                         gamma_off,
                         rot_axis,
                         dx,
                         dy,
                         wx,
                         wy,
                         tx_ins,
                         ty_ins,
                         tout_offsets=np.linspace(-10, 10, 201) * np.pi / 180)
dmd.plot_2d_sim(data2d, save_dir=None)

plt.show()
