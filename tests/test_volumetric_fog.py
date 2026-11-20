import pytest
from render import volumetric_fog as mod

CASES = [
    ("index", 'm.froxel_index(2, 3, 4, 8, 8) == 282'),
    ("slice", 'm.slice_depth(3, 0.1, 100, 4) == approx(0.1 * (1000 ** 0.75))'),
    ("reproj", 'm.reproject_fog((0.5, 0.5), (0.1, 0.0), (0.0, 0.2)) == approx((0.4, 0.3))'),
    ("bilateral", 'm.bilateral_weight(0.1, 0.1) == approx(0.367879, rel=1e-5)'),
    ("cascade", 'm.cascade_select(50, [10, 30, 80, 200]) == 2'),
    ("hg", 'm.henyey_greenstein(0.5, 0.0) == approx(1 / (4 * 3.141592653589793))'),
    ("blue", 'm.blue_noise_offset(5, 3) == approx(0.9375)'),
    ("transparent", 'm.blend_transparent(0.8, 0.2) == approx(0.64)'),
    ("barrier", 'm.barrier_needed("compute", "render") is True'),
    ("merge", 'm.light_merge(12, 8) == 8'),
    ("underwater", 'm.underwater_density(0.1, True) == approx(0.3)'),
    ("temporal", 'm.temporal_weight(0.5, 0.9, 0.2) == approx(0.58)'),
    ("half", 'm.fog_half_res(7, 5) == (4, 3)'),
    ("slices", 'm.depth_slice_count(100, 10) == 10'),
    ("extinction", 'm.scattering_extinction(2.0, 0.5) == approx(1.0)')
]

@pytest.mark.parametrize("name,expr", CASES, ids=[c[0] for c in CASES])
def test_contract(name, expr):
    assert eval(expr, {"m": mod, "approx": pytest.approx})
