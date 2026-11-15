# 输入协议与时间轴

> 路径说明（2026-11-12）：Python 命令以 `NeuroMotion_GUI` 为工作目录，[output/](../NeuroMotion_GUI/output)、[sessions/](../NeuroMotion_GUI/sessions)、[scripts/](../NeuroMotion_GUI/scripts) 均相对于该目录；MCU 工程位于 [../NeuroMotion_MCU/](../NeuroMotion_MCU)。计划、执行状态和历史验证报告另存本地，不随源码发布。

## 采集来源

客户端已支持实时串口接收，也兼容已有记录文件。NUCLEO-F446RE 固件以 1 kSPS 采集 ADS1292R 两路信号，每条记录保存样本序号和 MCU 同步状态。串口为 921600 baud、8N1；二进制帧以 `0xA5` 开始、`0x5A` 结束，地址 `0x11`，负载 240 字节、12 条记录，每条 20 字节。记录内为小端 uint32 样本序号、两路 float32 μV、offset 12 同步字节、offset 16 uint32 累计丢样计数。记录端同时保存串口二进制和解析 CSV，详见 [GUI 使用说明](../NeuroMotion_GUI/README.md)。

同步起始码为 `1110010`，符号宽度 500 ms，然后输出 1 Hz、50% 占空比方波。结束码为 `0001101`。固件先调用 `SyncSignal_Start()`，再调用 `ADS1292_Start()`；后者将样本序号清零，因此首条 EMG 往往已处于高电平。只有会话从接近样本 0 开始且后续完整符号匹配时，软件才允许反推不可见的首次上升沿。

## CSV

CSV 编码为 UTF-8，可含 BOM。以下两种表头均支持：

```csv
sampleIndex,ch1,ch2,sync,dropped_samples,pc_missing_samples,high_pass_hz,low_pass_hz,notch_50hz
sample_index,time_sec,ch1_raw_uv,ch2_raw_uv,ch1_uv,ch2_uv,sync,dropped_samples,sample_rate_hz,high_pass_hz,low_pass_hz,notch_50hz
```

样本序号必须为 uint32 范围内的整数；`sync` 必须严格为 0 或 1；两路数值必须有限。重复序号和非法计数会拒绝；序号下降表示新会话，不能跨会话对齐。输出的源数据行区间为从 0 开始、左闭右开的数据行范围，不计表头。累计计数缺列时按 0 处理，存在该列但包含空值、非整数或负值时拒绝。

同时存在 raw 与 filtered 通道时，主数值支路采用 `ch1_raw_uv/ch2_raw_uv`、`signal_kind=raw`，并在融合表中保留单独的滤波列。仅有 `ch1_uv/ch2_uv` 时标记 `filtered_or_unspecified`；WinForms `ch1/ch2` 标记 `winforms_filtered_or_unspecified`。旧文件缺失的原始支路留空，不伪造。新版记录还包含 `segment_id`、`filter_revision`、`despike_enabled`、`despike_replaced` 和 `quality_flag`，滤波生效事件见同目录的 `filter_events.jsonl`。配置的名义采样率必须与 CSV 元数据一致。

## 视频与标定

视频必须在起始编码之前开始，看到完整首次上升沿，且 LED ROI 不包含运动反光点。视频使用实际解码帧时间戳；时间戳无效时默认拒绝。只有用户明确确认固定帧率并启用 `allow_constant_fps_fallback`，才允许以 FPS 重建时间，并在报告中留警告。明显长于正常帧间隔的同步区段缺口会拒绝，较小缺口记录到 `data_gaps.csv`。

LED ROI 坐标属于原始完整帧；反光点 ROI、检测结果和单应矩阵属于去畸变完整帧。标定 JSON 必须包含与视频一致的 `image_size` 和有限重投影 RMS。内参需有 `K`、`dist`、`rms_reprojection_px`；单应矩阵需有 `H`、`coordinate_system=undistorted_full_frame`、`rms_reprojection_mm`。默认质量门槛分别为 2 px 和 1 mm，可显式配置。

## 映射与输出质量

以起始编码首位变高为公共零点。稳定段拟合 `sample_index = a × video_time_s + b`；按帧表的公共时间为 `video_time_s - video_start_s`，样本表为 `(sample_index - fitted_start_sample) / a`。`fitted_start_sample = a × video_start_s + b`，所以两种表严格使用同一映射；另保留按符号时长反推的起点作为独立一致性检查。

只匹配起始编码后的稳定方波边沿，至少 10 个；覆盖区间内的数量、极性、顺序和半周期须一致。两端均无结束码时，允许另一端在覆盖区间外的正常尾部边沿，报告记录未匹配尾部数量。起始/结束位置须与拟合一致，结束码仅一端出现也拒绝。新版正式验收的 RMS 和最大残差均不得超过一帧；历史离线计划的最大两帧门槛已收紧。每个匹配边沿的误差见 `ttl_edge_matches.csv`；这不等于已经测得 EMG 与动作的绝对延迟。

EMG 默认只在序号连续的相邻样本间插值（`max_emg_gap_samples=1`）；跨缺样时留空并标记 `emg_gap`。只有显式增大该参数才允许跨相应短缺口。轨迹只在可信且间隔不超过 `max_motion_gap_s` 的观测之间插值，帧覆盖外不外推。长时间丢失后重新分配的标记 ID 持续标为 `reacquired_unverified`，其坐标可供人工检查，但不会继续生成假定身份已确认的样本轨迹。

逐帧结果与轨迹使用磁盘暂存、固定 4096 行缓冲，完成后清理暂存目录；仅保留正在解码的画面。EMG CSV 仍整体载入内存。内存验收报告单列 Python 元数据堆，不把它称为整机总内存或解码器峰值内存。
