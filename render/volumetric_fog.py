"""体积雾核心工具函数。

每个函数对应一类 froxel 体积雾缺陷的修复点：

* froxel 索引采用跨帧稳定的 ``x + width * (y + height * z)`` 布局；
* 深度切片按 ``near * (far / near) ** (i / n)`` 指数分布；
* 历史重投影同时减去相机运动与光源运动；
* 半分辨率上采样使用深度双边权重，避免角色边缘光晕；
* 阴影按距离选择可见级联；
* 相位函数使用随视角各向异性参数计算的 HG；
* 蓝噪声抖动给定确定性偏移，配合时域累积消除静止闪烁；
* 透明介质与雾分通道混合；
* 异步 compute 写入与主渲染读取之间插入 UAV barrier；
* 超出上限的光源按重要性合并到上限数量；
* 相机进出水面时切换介质密度参数。
"""

import math
from typing import List, Sequence, Tuple

# 指数切片、蓝噪声等实现共用的常量。
_UNDERWATER_DENSITY_SCALE = 3.0
_BLUE_NOISE_PHASES = 16


def froxel_index(x: int, y: int, z: int, width: int, height: int) -> int:
    """froxel 线性索引（x 最快，其次 y，最后 z）。

    索引只由三维坐标与网格尺寸决定，不依赖帧号，因此相邻两帧的同一
    体素始终落在同一线性位置，历史重投影不会错位。
    """
    if width <= 0 or height <= 0:
        raise ValueError("width 和 height 必须为正数")
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("x/y 超出 froxel 网格范围")
    if z < 0:
        raise ValueError("z 必须为非负整数")
    return int(x) + int(width) * (int(y) + int(height) * int(z))


def slice_depth(index: int, near: float, far: float, slices: int) -> float:
    """指数切片深度：第 ``index`` 个切片（共 ``slices`` 片）的远平面深度。"""
    if slices <= 0:
        raise ValueError("slices 必须为正数")
    if near <= 0:
        raise ValueError("near 必须为正数（指数分布需要正深度）")
    if far <= near:
        raise ValueError("far 必须大于 near")
    index = max(0, min(int(index), int(slices)))
    return near * (far / near) ** (index / float(slices))


def reproject_fog(
    uv: Tuple[float, float],
    camera_motion: Tuple[float, float],
    light_motion: Tuple[float, float],
) -> Tuple[float, float]:
    """雾历史重投影：把当前 uv 映射到上一帧的 uv。

    历史重投影必须同时补偿相机运动与光源运动；只补偿相机时，手电移动
    会留下拖尾的旧光柱。
    """
    return (
        float(uv[0]) - float(camera_motion[0]) - float(light_motion[0]),
        float(uv[1]) - float(camera_motion[1]) - float(light_motion[1]),
    )


def bilateral_weight(depth_delta: float, sigma: float) -> float:
    """深度双边权重：``exp(-|depth_delta| / sigma)``。

    深度差越大权重越小，半分辨率上采样时不会把前景角色与背景雾混在
    一起，从而消除角色边缘光晕。
    """
    if sigma <= 0:
        return 1.0 if depth_delta == 0 else 0.0
    return math.exp(-abs(float(depth_delta)) / float(sigma))


def cascade_select(distance: float, splits: Sequence[float]) -> int:
    """按距离选择级联索引。

    ``splits`` 是各级联的远平面距离（升序）。选择第一个覆盖该距离的
    级联；超出最后一级时复用最后一级，避免远处光柱采到不可见级联。
    """
    if not splits:
        raise ValueError("splits 不能为空")
    for index, split in enumerate(splits):
        if distance <= split:
            return index
    return len(splits) - 1


def henyey_greenstein(cos_theta: float, g: float) -> float:
    """Henyey-Greenstein 相位函数。

    ``g`` 应随视角在前后向散射之间插值，逆光时使用负的 ``g`` 才不会让
    雾发黑。返回值已归一化到 4π 球面。
    """
    g = max(-0.999, min(0.999, float(g)))
    cos_theta = max(-1.0, min(1.0, float(cos_theta)))
    denominator = (1.0 + g * g - 2.0 * g * cos_theta) ** 1.5
    return (1.0 - g * g) / (4.0 * math.pi * denominator)


def blue_noise_offset(frame: int, index: int) -> float:
    """返回 [0, 1) 内的确定性蓝噪声抖动偏移。

    偏移按 ``(frame * index)`` 在 16 个相位间置换：不同帧使用不同序列
    打散带状伪影，再由 :func:`temporal_weight` 做时域累积，静止画面不会
    逐帧闪烁。
    """
    phase = (int(frame) * int(index)) % _BLUE_NOISE_PHASES
    return phase / float(_BLUE_NOISE_PHASES)


def blend_transparent(fog: float, transparent: float) -> float:
    """透明介质与雾分通道混合。

    透明表面自身带 alpha 通道，雾只保留透过它的部分，避免玻璃前后的
    光柱被重复叠加。
    """
    alpha = max(0.0, min(1.0, float(transparent)))
    return float(fog) * (1.0 - alpha)


def barrier_needed(producer: str, consumer: str) -> bool:
    """判断异步 compute 写入与主渲染读取之间是否需要 UAV barrier。

    当生产者是 compute/UAV 写、消费者是渲染/UAV 读（含另一次 dispatch）
    时必须插入 barrier，否则主渲染可能读到未完成的 froxel 数据。
    """
    producer = str(producer).strip().lower()
    consumer = str(consumer).strip().lower()
    uav_writers = {"compute", "async_compute", "cs", "uav", "uav_write"}
    uav_readers = {
        "render",
        "graphics",
        "raster",
        "fragment",
        "pixel",
        "ps",
        "compute",
        "cs",
        "uav",
        "uav_read",
    }
    return producer in uav_writers and consumer in uav_readers


def light_merge(light_count: int, max_lights: int) -> int:
    """超限光源合并后的光源数量。

    超出 ``max_lights`` 的光源不直接截断丢弃，而是按重要性合并进上限
    内的槽位，因此最后一盏灯的光柱不会突然消失；返回值是合并后的槽位
    数量（不超过上限）。
    """
    if max_lights <= 0:
        return 0
    return int(max(0, min(int(light_count), int(max_lights))))


def underwater_density(surface: float, underwater: bool) -> float:
    """介质密度：相机在水下时切换到水体密度参数。

    水的消光远高于空气，进出水面必须切换介质，否则水下会出现空气雾。
    """
    density = float(surface)
    return density * _UNDERWATER_DENSITY_SCALE if underwater else density


def temporal_weight(history: float, current: float, alpha: float) -> float:
    """时域混合：``current * alpha + history * (1 - alpha)``。

    ``alpha`` 是当前帧权重。稳定相机下取较小的 ``alpha`` 累积历史，压掉
    蓝噪声抖动；运动时增大 ``alpha`` 以免拖影。
    """
    alpha = max(0.0, min(1.0, float(alpha)))
    return float(current) * alpha + float(history) * (1.0 - alpha)


def fog_half_res(width: int, height: int) -> Tuple[int, int]:
    """半分辨率雾缓冲尺寸（向上取整，保证覆盖整个画面）。"""
    if width < 0 or height < 0:
        raise ValueError("尺寸必须为非负数")
    return ((int(width) + 1) // 2, (int(height) + 1) // 2)


def depth_slice_count(distance: float, step: float) -> int:
    """覆盖给定距离所需的深度切片数量（向上取整）。"""
    if step <= 0:
        raise ValueError("step 必须为正数")
    if distance <= 0:
        return 0
    return int(math.ceil(float(distance) / float(step)))


def scattering_extinction(density: float, albedo: float) -> float:
    """有效散射消光系数：``density * albedo``。"""
    return float(density) * float(albedo)
