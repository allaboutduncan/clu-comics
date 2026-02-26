"""Tests for helpers.py -- is_hidden, image math, and utility functions."""
import pytest
import math
from unittest.mock import patch, MagicMock


# ===== is_hidden =====

class TestIsHidden:

    def test_dot_prefix(self):
        from helpers import is_hidden
        assert is_hidden("/some/path/.hidden_file") is True

    def test_underscore_prefix(self):
        from helpers import is_hidden
        assert is_hidden("/some/path/_prefixed") is True

    def test_normal_file(self, tmp_path):
        from helpers import is_hidden
        f = tmp_path / "normal_file.txt"
        f.write_text("hi")
        assert is_hidden(str(f)) is False

    def test_dot_directory(self):
        from helpers import is_hidden
        assert is_hidden("/some/.git") is True

    def test_nested_normal_file(self, tmp_path):
        from helpers import is_hidden
        # Only checks the basename, not parent dirs
        d = tmp_path / ".git"
        d.mkdir()
        f = d / "config"
        f.write_text("hi")
        assert is_hidden(str(f)) is False

    def test_dsstore(self):
        from helpers import is_hidden
        assert is_hidden("/path/.DS_Store") is True

    def test_macosx_dir(self):
        from helpers import is_hidden
        assert is_hidden("/path/_MACOSX") is True


# ===== apply_gamma =====

class TestApplyGamma:

    def test_gamma_1_is_identity(self):
        from helpers import apply_gamma
        from PIL import Image

        img = Image.new("RGB", (10, 10), color=(128, 128, 128))
        result = apply_gamma(img, gamma=1.0)
        # With gamma=1.0, output should be very close to input
        pixel = result.getpixel((5, 5))
        assert pixel == (128, 128, 128)

    def test_gamma_returns_image(self):
        from helpers import apply_gamma
        from PIL import Image

        img = Image.new("RGB", (10, 10), color=(100, 150, 200))
        result = apply_gamma(img, gamma=0.9)
        assert result.size == (10, 10)

    def test_gamma_brightens_with_high_value(self):
        from helpers import apply_gamma
        from PIL import Image

        img = Image.new("RGB", (1, 1), color=(100, 100, 100))
        result = apply_gamma(img, gamma=2.0)
        pixel = result.getpixel((0, 0))
        # Higher gamma param → inv=1/gamma is small → brightens
        assert pixel[0] > 100

    def test_gamma_darkens_with_low_value(self):
        from helpers import apply_gamma
        from PIL import Image

        img = Image.new("RGB", (1, 1), color=(200, 200, 200))
        result = apply_gamma(img, gamma=0.5)
        pixel = result.getpixel((0, 0))
        # Lower gamma param → inv=1/gamma is large → darkens
        assert pixel[0] < 200


# ===== modified_s_curve_lut =====

class TestModifiedSCurveLut:

    def test_returns_256_entries(self):
        from helpers import modified_s_curve_lut
        lut = modified_s_curve_lut()
        assert len(lut) == 256

    def test_starts_near_zero(self):
        from helpers import modified_s_curve_lut
        lut = modified_s_curve_lut(shadow_lift=0.0)
        assert lut[0] == 0

    def test_ends_at_255(self):
        from helpers import modified_s_curve_lut
        lut = modified_s_curve_lut()
        assert lut[255] == 255

    def test_all_values_in_range(self):
        from helpers import modified_s_curve_lut
        lut = modified_s_curve_lut()
        for val in lut:
            assert 0 <= val <= 255

    def test_shadow_lift_raises_dark_values(self):
        from helpers import modified_s_curve_lut
        lut_no_lift = modified_s_curve_lut(shadow_lift=0.0)
        lut_with_lift = modified_s_curve_lut(shadow_lift=0.5)
        # Dark values (index 10-50) should be higher with lift
        for i in range(10, 50):
            assert lut_with_lift[i] >= lut_no_lift[i]

    def test_monotonic_in_upper_range(self):
        from helpers import modified_s_curve_lut
        lut = modified_s_curve_lut()
        # Values should generally increase (monotonic) in the 128-255 range
        for i in range(128, 255):
            assert lut[i + 1] >= lut[i]
