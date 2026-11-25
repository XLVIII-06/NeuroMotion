# 输入协议与时间轴

> 路径说明（2026-11-12）：Python 命令以 `NeuroMotion_GUI` 为工作目录，[output/](../NeuroMotion_GUI/output)、[sessions/](../NeuroMotion_GUI/sessions)、[scripts/](../NeuroMotion_GUI/scripts) 均相对于该目录；MCU 工程位于 [../NeuroMotion_MCU/](../NeuroMotion_MCU)。计划、执行状态和历史验证报告另存本地，不随源码发布。

## 采集来源

客户端支持实时串口、二进制重放和明确标识的模拟输入，自动识别双通道 v1 / 四通道 v2；同一连接中不能混用两种协议。串口均为 921600 baud、8N1。四通道接线和文件作用见 [四通道说明](FOUR_CHANNEL_GUIDE.md)，字节定义见 [v2 协议](PROTOCOL_V2.md)。

原双通道固件以 1 kSPS 采集单块 ADS1292R，每条记录保存样本序号和 MCU 同步状态。v1 二进制帧以 `0xA5` 开始、`0x5A` 结束，地址 `0x11`，负载 240 字节、12 条记录，每条 20 字节。记录内为小端 uint32 样本序号、两路 float32 μV、offset 12 同步字节、offset 16 uint32 累计丢样计数。记录端同时保存串口二进制和解析 CSV。

四通道 v2 以 `NM2!` 开始，包含版本、类型、长度、帧序号和 CRC32。每条原生记录 28 字节，保留模块 ID、独立原生序号、uint64 DRDY 微秒时间、两路有符号 ADC 码、原始状态及错误标记。TTL 实测边沿由单独的 SYNC_EDGE 帧传输。PC 根据 META 中的增益和参考电压换算 μV，每秒更新独立时钟模型，生成约延后 0.5 秒的公共网格；PC 到达时间不用于校时。

同步起始码为 `1110010`，符号宽度 500 ms，然后输出 1 Hz、50% 占空比方波。结束码为 `0001101`。原双通道固件先启动 TTL，再启动 ADS 并将样本序号清零，因此首条 EMG 往往已处于高电平。仅会话从接近样本 0 开始且后续完整符号匹配时，旧格式允许反推不可见的首次上升沿。四通道使用 PA8→PB10 的实际捕获边沿和 `ttl_start_us`，不依此推测硬件锚点。

## CSV

CSV 编码为 UTF-8，可含 BOM。以下两种表头均支持：

```csv
sampleIndex,ch1,ch2,sync,dropped_samples,pc_missing_samples,high_pass_hz,low_pass_hz,notch_50hz
sample_index,time_sec,ch1_raw_uv,ch2_raw_uv,ch1_uv,ch2_uv,sync,dropped_samples,sample_rate_hz,high_pass_hz,low_pass_hz,notch_50hz
```

样本序号必须为 uint32 范围内的整数；`sync` 必须严格为 0 或 1；两路数值必须有限。重复序号和非法计数会拒绝；序号下降表示新会话，不能跨会话对齐。输出的源数据行区间为从 0 开始、左闭右开的数据行范围，不计表头。累计计数缺列时按 0 处理，存在该列但包含空值、非整数或负值时拒绝。

同时存在 raw 与 filtered 通道时，主数值支路采用 `ch1_raw_uv/ch2_raw_uv`、`signal_kind=raw`，并在融合表中保留单独的滤波列。仅有 `ch1_uv/ch2_uv` 时标记 `filtered_or_unspecified`；WinForms `ch1/ch2` 标记 `winforms_filtered_or_unspecified`。旧文件缺失的原始支路留空，不伪造。新版记录还包含 `segment_id`、`filter_revision`、`despike_enabled`、`despike_replaced` 和 `quality_flag`，滤波生效事件见同目录的 `filter_events.jsonl`。配置的名义采样率必须与 CSV 元数据一致。

四通道 CSV 使用 `format_version=2`。`segment_id` 区分 MCU 复位，`sample_index` 为当前记录的公共网格序号，`mcu_time_us` 在每个会话内严格增加 1000；`time_sec` 为该记录网格经过的时间，`common_time_s=(mcu_time_us-ttl_start_us)/1e6`。未取得 TTL 锚点时公共时间为 NaN，不能用于正式视频同步。该网格使用 MCU 名义微秒时标，尚未做绝对 SI 秒校准。

`ch1_aligned_uv` 到 `ch4_aligned_uv` 是未加 EMG 滤波的软件重采样值；`ch1_filtered_uv` 到 `ch4_filtered_uv` 为滤波支路。CH1/CH2 来自 A，CH3/CH4 来自 B。`quality_ch1..4` 与 `filter_quality_ch1..4` 分别描述每路质量；缺失值保留 NaN，不使其余模块的正常值无效。另保存 `sync`、`sync_valid`、A/B 模型修订、滤波修订和设置。只有 `valid` 质量可代表有限测量值。

原生码见同目录 `native_samples.csv`，包含 `segment_id,device,native_index,drdy_time_us,ch1_code,ch2_code,ch1_native_uv,ch2_native_uv,adc_status,flags,known_dropped,valid`。`device=0/1` 分别表示 A/B，原生 CSV 中的 ch1/ch2 是板内通道。转换公式为 `code × vref_uv / (2^23 × gain)`。错误记录的码仍保留诊断信息，不能当作有效零值。

四通道正式对齐必须保留同目录 `sync_edges.csv`，其字段为 `segment_id,edge_index,mcu_time_us,level`。该台账用于修正网格量化后的边沿位置；缺失、不连续或与网格 TTL 矛盾时拒绝正式对齐。`clock_models.jsonl` 记录持续模型和生效点，`clock_reference.json` 保留每个 MCU 会话约 20 秒固定参考及偏移/频差；文件作用详见 [四通道说明](FOUR_CHANNEL_GUIDE.md)。

## 视频与标定

视频必须在起始编码之前开始，看到完整首次上升沿，且 LED ROI 不包含运动反光点。视频使用实际解码帧时间戳；时间戳无效时默认拒绝。只有用户明确确认固定帧率并启用 `allow_constant_fps_fallback`，才允许以 FPS 重建时间，并在报告中留警告。明显长于正常帧间隔的同步区段缺口会拒绝，较小缺口记录到 `data_gaps.csv`。

LED ROI 坐标属于原始完整帧；反光点 ROI、检测结果和单应矩阵属于去畸变完整帧。标定 JSON 必须包含与视频一致的 `image_size` 和有限重投影 RMS。内参需有 `K`、`dist`、`rms_reprojection_px`；单应矩阵需有 `H`、`coordinate_system=undistorted_full_frame`、`rms_reprojection_mm`。默认质量门槛分别为 2 px 和 1 mm，可显式配置。

## 映射与输出质量

以起始编码首位变高为公共零点。稳定段拟合 `sample_index = a × video_time_s + b`。旧双通道按帧表的公共时间为 `video_time_s - video_start_s`，样本表为 `(sample_index - fitted_start_sample) / a`，其中 `fitted_start_sample = a × video_start_s + b`。四通道以 MCU 为主时钟：样本公共时间直接读取 CSV；视频公共时间为 `(a × video_time_s + b - master_start_sample) / 1000`。两种表、位置插值及同步回放均采用同一版本对应的映射，不混用视频秒与 MCU 秒。

只匹配起始编码后的稳定方波边沿，至少 10 个；覆盖区间内的数量、极性、顺序和半周期须一致。两端均无结束码时，允许另一端在覆盖区间外的正常尾部边沿，报告记录未匹配尾部数量。起始/结束位置须与拟合一致，结束码仅一端出现也拒绝。新版正式验收的 RMS 和最大残差均不得超过一帧；历史离线计划的最大两帧门槛已收紧。每个匹配边沿的误差见 `ttl_edge_matches.csv`；这不等于已经测得 EMG 与动作的绝对延迟。

EMG 默认只在序号连续的相邻样本间插值（`max_emg_gap_samples=1`）；跨缺样时留空并标记 `emg_gap`。只有显式增大该参数才允许跨相应短缺口。轨迹只在可信且间隔不超过 `max_motion_gap_s` 的观测之间插值，帧覆盖外不外推。长时间丢失后重新分配的标记 ID 持续标为 `reacquired_unverified`，其坐标可供人工检查，但不会继续生成假定身份已确认的样本轨迹。

逐帧结果与轨迹使用磁盘暂存、固定 4096 行缓冲，完成后清理暂存目录；仅保留正在解码的画面。旧双通道 EMG CSV 仍整体载入内存；四通道使用 4096 行分块校验及磁盘映射，回放使用磁盘窗口索引。内存验收报告单列 Python 元数据堆，不把它称为整机总内存或解码器峰值内存。

## BL-422I TTL 与刺激标注

BL 输入允许 GB18030 或 UTF-8/BOM。元数据必须含声明样本数和采样率；表头含 `序号,时间,2通道数据(mV),事件`。时间接受 `MM:SS.fff` 或 `HH:MM:SS.fff`，最多微秒精度；序号连续、时间严格递增并与采样率相符。电压单位严格为 mV；ELE 不属于本版输入。采样通过 4096 行缓冲写入磁盘映射，不整体载入 BL 电压。

`Stimulate: 0.00mA`、`Stimulate: -0.50mA` 等为单脉冲开始标注，电流只是设定值。输出 `event_id` 由源内容哈希和序号确定，并保留源行号、源序号、原文、BL 秒、设定 mA、脉宽 ms、备注、参数来源及实际输出时刻未校准标志。纸面参数文件使用 `event_id,pulse_width_ms,notes`；空白脉宽为未知，非空必须正且有限，不允许借参数表修改原始标注或电流。

模拟 TTL 用双平台和滞回确定状态，再以各边沿局部高低平台的 50% 插值得到时间。起点取实际首个编码上升沿，不用名义 1.5 s 回推。按完整起始码和稳定边沿顺序匹配；BL 允许反相。默认 BL→MCU 留一验证门槛为 RMS ≤1 ms、最大 ≤2 ms，仿射不满足时才评估单调分段映射；不能用分段锚点自身的零残差作为验收。映射不向可信锚点外推。

加入 BL 后，两种主表裁剪至 EMG、视频、BL 可信覆盖交集，新增 `stim_event_count` 和 JSON 数组 `stim_event_ids`；同一时间格内多事件不覆盖。独立 `stimulus_events.csv` 保留全部事件，覆盖外公共时间可为空并带状态。`bl_ttl.csv` 保存原始 mV 与逻辑 TTL，`bl_ttl_edge_matches.csv` 保存稳定匹配及各边沿留一残差，`bl_clock_map.json` 保存全部锚点、模型、覆盖与判定。

## 可搬移的处理结果

新版 `result_manifest.json` schema v1 以相对路径和 SHA256 引用处理产物，视频默认引用原路径；`replay_signals.csv` 每样本一行，保存两/四路主值、原始/滤波可用值及质量。v5 磁盘回放索引从这些处理产物重建，不重读原始 EMG/BL；旧版无清单结果仍使用 v4 流程。四通道 raw 字段不可用时为 NaN，原生 ADC 码仍在采集资料中。

视频重定位必须校验相同内容。便携包复制视频、信号、帧、轨迹、事件、标定和报告，视频改为相对路径；原始采集文件可选归档。清单完整后才视为可用包，取消时保留不完整目录以便检查。处理产物被修改或缺失时拒绝读取旧缓存，应重新生成结果；不静默修补哈希。像素模式 `coordinate_mode=pixel` 明确保留毫米 NaN；物理尺度需匹配的相机内参和同一运动平面单应矩阵。
