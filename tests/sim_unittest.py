"""
Tests for some important functions from sim_reconstruction.py
"""
import unittest
import numpy as np
from scipy import fft
import sim_reconstruction as sim
import analysis_tools as tools

class TestSIM(unittest.TestCase):

    def setUp(self):
        pass

    def test_fit_modulation_frq(self):
        """
        Test fit_modulation_frq() function

        :return:
        """
        # set parameters
        options = {'pixel_size': 0.065, 'wavelength': 0.5, 'na': 1.3}
        fmax = 1 / (0.5 * options["wavelength"] / options["na"])
        nx = 512
        f = 1 / 0.25
        angle = 30 * np.pi / 180
        frqs = [f * np.cos(angle), f * np.sin(angle)]
        phi = 0.2377747474

        # create sample image
        x = options['pixel_size'] * np.arange(nx)
        y = x
        xx, yy = np.meshgrid(x, y)
        m = 1 + 0.5 * np.cos(2 * np.pi * (frqs[0] * xx + frqs[1] * yy) + phi)

        mft = fft.fftshift(fft.fft2(fft.ifftshift(m)))
        frq_extracted, mask, = sim.fit_modulation_frq(mft, mft, options["pixel_size"], fmax, exclude_res=0.6)

        self.assertAlmostEqual(np.abs(frq_extracted[0]), np.abs(frqs[0]), places=5)
        self.assertAlmostEqual(np.abs(frq_extracted[1]), np.abs(frqs[1]), places=5)

    def test_fit_phase_realspace(self):
        """
        Test fit_phase_realspace()
        :return:
        """

        # set parameters
        dx = 0.065
        nx = 2048
        f = 1 / 0.25
        angle = 30 * np.pi/180
        frqs = [f * np.cos(angle), f * np.sin(angle)]
        phi = 0.2377747474

        # create sample image with origin at edge
        x_edge = dx * np.arange(nx)
        y_edge = x_edge
        xx_edge, yy_edge = np.meshgrid(x_edge, y_edge)
        m_edge = 1 + 0.2 * np.cos(2 * np.pi * (frqs[0] * xx_edge + frqs[1] * yy_edge) + phi)

        phase_guess_edge = sim.get_phase_realspace(m_edge, frqs, dx, phase_guess=0, origin="edge")

        self.assertAlmostEqual(phi, float(phase_guess_edge), places=5)

        # create sample image with origin in center
        x_center = tools.get_fft_pos(nx, dx)
        y_center = x_center
        xx_center, yy_center = np.meshgrid(x_center, y_center)
        m_center = 1 + 0.2 * np.cos(2 * np.pi * (frqs[0] * xx_center + frqs[1] * yy_center) + phi)

        phase_guess_center = sim.get_phase_realspace(m_center, frqs, dx, phase_guess=0, origin="center")

        self.assertAlmostEqual(phi, float(phase_guess_center), places=5)

    def test_estimate_phase(self):
        """
        Test estimate_phase() function, which guesses phase from value of image FT
        :return:
        """
        # set parameters
        dx = 0.065
        nx = 2048
        f = 1 / 0.25
        angle = 30 * np.pi / 180
        frqs = [f * np.cos(angle), f * np.sin(angle)]
        phi = 0.2377747474

        # create sample image with origin in center
        x_center = tools.get_fft_pos(nx, dx)
        y_center = x_center
        xx_center, yy_center = np.meshgrid(x_center, y_center)
        m_center = 1 + 0.2 * np.cos(2 * np.pi * (frqs[0] * xx_center + frqs[1] * yy_center) + phi)

        m_center_ft = fft.fftshift(fft.fft2(fft.ifftshift(m_center)))

        phase_guess_center = sim.get_phase_ft(m_center_ft, frqs, dx)

        self.assertAlmostEqual(phi, float(phase_guess_center), places=5)

    def test_band_mixing_mat(self):
        """
        Test that the real-space and Fourier-space pattern generation models agree.

        i.e. that D_i(x) = amp * (1 + m * cos(2*pi*f + phi_i)) * S(r)
        matches the result given using the fourier space matrix
        [[D_1(k)], [D_2(k)], [D_3(k)]] = M * [[S(k)], [S(k-p)], [S(k+p)]]

        :return:
        """

        # set values for SIM images
        dx = 0.065
        frqs = np.array([[3.5785512, 2.59801082]])
        phases = np.array([[0, 2 * np.pi / 3, 3 * np.pi / 3]])
        mods = np.array([0.85, 0.26, 0.19])
        amps = np.array([[1.11, 1.23, 0.87]])
        nangles, nphases = phases.shape

        # ground truth image
        ny = 512
        nx = ny
        gt = np.random.rand(ny, nx)

        # calculate sim patterns using real space method
        x = tools.get_fft_pos(nx, dx)
        y = tools.get_fft_pos(ny, dx)
        xx, yy = np.meshgrid(x, y)

        sim_rs = np.zeros((nangles, nphases, ny, nx))
        sim_rs_ft = np.zeros((nangles, nphases, ny, nx), dtype=complex)
        for ii in range(nangles):
            for jj in range(nphases):
                pattern = amps[ii, jj] * (1 + mods[ii] * np.cos(2*np.pi * (xx * frqs[ii, 0] + yy * frqs[ii, 1]) + phases[ii, jj]))
                sim_rs[ii, jj] = gt * pattern
                sim_rs_ft[ii, jj] = fft.fftshift(fft.fft2(fft.ifftshift(sim_rs[ii, jj])))

        # calculate SIM patterns using Fourier space method
        # frq shifted gt images
        gt_ft_shifted = np.zeros((nangles, nphases, ny, nx), dtype=complex)
        for ii in range(nangles):
            gt_ft_shifted[ii, 0] = fft.fftshift(fft.fft2(fft.ifftshift(gt)))
            gt_ft_shifted[ii, 1] = tools.translate_ft(gt_ft_shifted[ii, 0], -frqs[ii], dx)
            gt_ft_shifted[ii, 2] = tools.translate_ft(gt_ft_shifted[ii, 0], frqs[ii], dx)

        sim_fs_ft = np.zeros(gt_ft_shifted.shape, dtype=complex)
        for ii in range(nangles):
            kmat = sim.get_band_mixing_matrix(phases[ii], mods[ii], amps[ii])
            sim_fs_ft[ii] = sim.image_times_matrix(gt_ft_shifted[ii], kmat)

        sim_fs_rs = np.zeros(gt_ft_shifted.shape)
        for ii in range(nangles):
            for jj in range(nphases):
                sim_fs_rs[ii, jj] = fft.fftshift(fft.ifft2(fft.ifftshift(sim_fs_ft[ii, jj]))).real

        np.testing.assert_allclose(sim_fs_ft, sim_rs_ft, atol=1e-10)
        np.testing.assert_allclose(sim_fs_rs, sim_rs, atol=1e-12)

    def test_band_mixing_mat_jac(self):
        """
        test jacobian of band mixing matrix
        @return:
        """
        phases = [0, 2*np.pi/3 - 0.89243, 4*np.pi/3 + 0.236]
        amps = [0.78, 0.876, 0.276]
        m = 0.777
        params = np.array(phases + amps + [m])
        ds = 1e-8

        jac = sim.get_band_mixing_matrix_jac(phases, m, amps)

        jac_est = []
        def get_mat(p): return sim.get_band_mixing_matrix([p[0], p[1], p[2]], p[6], [p[3], p[4], p[5]])
        for ii in range(len(params)):
            params_temp = np.array(params, copy=True)
            params_temp[ii] -= ds
            jac_est.append(1 /ds * (get_mat(params) - get_mat(params_temp)))

        max_err = np.max([np.max(np.abs(jac[ii] - jac_est[ii])) for ii in range(len(params))])
        self.assertAlmostEqual(max_err, 0, places=7)

    def test_band_mixing_mat_inv(self):
        """
        test inverse mixing matrix gives correct result
        @return:
        """
        phases = [0, 2 * np.pi / 3 - 0.89243, 4 * np.pi / 3 + 0.236]
        amps = [0.78, 0.876, 0.276]
        m = 0.777

        mat = sim.get_band_mixing_matrix(phases, m, amps)
        mat_inv = sim.get_band_mixing_inv(phases, m, amps)

        np.testing.assert_allclose(mat.dot(mat_inv), np.identity(mat.shape[0]), atol=1e-10)

    def test_band_mixing_mat_inv_jac(self):
        """
        test jacobian of band mixing matrix inverse
        @return:
        """
        phases = [0, 2 * np.pi / 3 - 0.89243, 4 * np.pi / 3 + 0.236]
        amps = [0.78, 0.876, 0.276]
        m = 0.777
        params = np.array(phases + amps + [m])
        ds = 1e-8

        jac = sim.get_band_mixing_inv_jac(phases, m, amps)

        jac_est = []
        def get_mat(p): return sim.get_band_mixing_inv([p[0], p[1], p[2]], p[6], [p[3], p[4], p[5]])

        for ii in range(len(params)):
            params_temp = np.array(params, copy=True)
            params_temp[ii] -= ds
            jac_est.append(1 / ds * (get_mat(params) - get_mat(params_temp)))

        max_err = np.max([np.max(np.abs(jac[ii] - jac_est[ii])) for ii in range(len(params))])
        self.assertAlmostEqual(max_err, 0, places=6)

    @unittest.skip("decided get_test_pattern() function does not belong. But should generate ")
    def test_test_patterns(self):
        """
        Generate SIM test patterns with no noise or OTF blurring, and verify that these can be reconstructed.
        :return:
        """
        gt = sim.get_test_pattern([600, 600])
        otf = np.ones(gt.shape)
        frqs = np.array([[0.21216974, 3.97739458],
                         [-3.17673383, 1.97618225],
                         [3.24054346, 1.8939568]])
        phases = np.zeros((3, 3))
        phases[:, 0] = 0
        phases[:, 1] = 2 * np.pi / 3
        phases[:, 2] = 4 * np.pi / 3
        mod_depths = np.array([[0.9, 0.8, 0.87],
                               [1, 0.98, 0.88],
                               [0.89, 0.912, 0.836]])
        amps = np.array([[1, 0.7, 1.1],
                         [1, 1.16, 0.79],
                         [1, 0.83, 1.2]])

        max_photons = 1
        sim_imgs, _ = sim.get_simulated_sim_imgs(gt, frqs, phases, mod_depths, max_photons,
                                              1, 0, 0, 0.065, amps, otf=otf, photon_shot_noise=False)
        nangles, nphases, ny, nx = sim_imgs.shape

        imgs_ft = np.zeros(sim_imgs.shape, dtype=complex)
        for ii in range(nangles):
            for jj in range(nphases):
                imgs_ft[ii, jj] = fft.fftshift(fft.fft2(fft.ifftshift(sim_imgs[ii, jj])))

        separated_components = sim.do_sim_band_separation(imgs_ft, phases, mod_depths, amps)

        gt_per_angle = np.zeros((nangles, ny, nx))
        for ii in range(nangles):
            gt_per_angle[ii] = fft.fftshift(fft.ifft2(fft.ifftshift(separated_components[ii, 0]))).real

            np.testing.assert_allclose(gt, gt_per_angle[ii], atol=1e-14)

if __name__ == "__main__":
    unittest.main()
