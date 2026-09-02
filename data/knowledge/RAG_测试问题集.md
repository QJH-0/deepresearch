# RAG 检索效果测试问题集

> 本问题集覆盖所有已入库的垂直行业文档（下载的 HTML 文档 + 生成的 PDF/Word/MD 文档），用于手动检测 DeepResearch RAG 系统的检索召回率和准确率。
>
> **使用方法**：将以下问题逐条输入到 DeepResearch 系统中，检查返回的检索结果是否命中了正确的文档片段。每个问题标注了【来源文档】和【期望关键词】，用于快速验证。

---

## 一、工业物联网（IIoT）边缘计算

> 来源文档：`IIoT_Edge_Computing_Guide.pdf/docx/md`、`wiki_industrial_iot.html`、`wiki_edge_computing.html`

| 编号 | 测试问题 | 期望关键词 | 难度 |
|------|----------|------------|------|
| Q1 | IIoT 与消费级 IoT 有什么区别？ | 可靠性、实时性、安全性、CPS | 简单 |
| Q2 | 边缘计算的三层架构是什么？ | 设备层、边缘层、云层 | 简单 |
| Q3 | K3s 是什么？它和标准 Kubernetes 有什么区别？ | 轻量级、70MB、边缘计算 | 中等 |
| Q4 | OPC UA 协议相比传统 OPC 有哪些改进？ | COM/DCOM、TCP、X.509、订阅发布 | 中等 |
| Q5 | OPC UA 信息模型中的 NodeId 是什么？ | 唯一标识、References | 中等 |
| Q6 | NAMUR 和 PackML 在 OPC UA 中扮演什么角色？ | Companion Specification、配套信息模型、跨厂商 | 困难 |
| Q7 | 滚动轴承的外圈故障频率 BPFO 计算公式是什么？ | 0.5、n、fr、d/D、cos | 困难 |
| Q8 | Isolation Forest 算法的核心思想和时间复杂度是什么？ | 随机划分、隔离、O(n log n) | 中等 |
| Q9 | IEC 62443 标准定义了哪些安全等级？SL2 适用于什么场景？ | SL1~SL4、中等资源、蓄意攻击 | 中等 |
| Q10 | Purdue 参考模型中 Level 3.5 是什么区域？ | DMZ、隔离区、OT和IT缓冲 | 中等 |

---

## 二、精准农业与智慧农业

> 来源文档：`Precision_Agriculture_Whitepaper.pdf/docx/md`、`wiki_precision_agriculture.html`

| 编号 | 测试问题 | 期望关键词 | 难度 |
|------|----------|------------|------|
| Q11 | 精准农业可以减少多少化肥和农药使用量？ | 20%、30% | 简单 |
| Q12 | 克里金法相比其他插值方法有什么优势？ | 地统计学、无偏最优估计、误差方差 | 中等 |
| Q13 | 半变异函数的块金值（Nugget）反映了什么？ | 小尺度变异、速效磷 | 困难 |
| Q14 | 变量施肥机是如何实现按位置变量施肥的？ | GPS、处方图、电液比例阀、排肥轮 | 中等 |
| Q15 | NDVI 的计算公式是什么？健康植被的 NDVI 值范围是多少？ | (NIR-Red)/(NIR+Red)、0.6~0.9 | 中等 |
| Q16 | 植保无人机 RTK-GPS 的航线精度是多少？ | 厘米级、±2cm | 中等 |
| Q17 | FDR 和 TDR 土壤水分传感器有什么区别？ | 频域反射、时域反射、介电常数、精度 | 困难 |
| Q18 | 作物水分胁迫指数 CWSI 的计算公式是什么？ | (Tc-Twet)/(Tdry-Twet)、冠层温度 | 困难 |
| Q19 | 智慧农业大数据平台的数据存储层使用哪些数据库？ | InfluxDB、PostGIS、时序、空间 | 中等 |
| Q20 | 150亩玉米田的土壤采样网格密度如何选择？ | 30m×30m、80个采样点 | 中等 |

---

## 三、稀土供应链与战略资源

> 来源文档：`Rare_Earth_Supply_Chain_Report.pdf/docx/md`、`wiki_rare_earth.html`

| 编号 | 测试问题 | 期望关键词 | 难度 |
|------|----------|------------|------|
| Q21 | 稀土元素包括哪17种元素？ | 15种镧系、钇、钪 | 简单 |
| Q22 | 轻稀土和重稀土的划分标准是什么？ | La~Eu、Gd~Lu+Y | 简单 |
| Q23 | 白云鄂博矿是什么类型的稀土矿？ | 轻稀土、铁-稀土-铌共生、露天 | 中等 |
| Q24 | 南方离子型稀土矿的提取工艺是什么？有什么环境风险？ | 原地浸矿、硫酸铵、氨氮污染 | 中等 |
| Q25 | 稀土分离中 P507 萃取剂是什么？分离系数 beta 通常在什么范围？ | 2-乙基己基磷酸、1.5~3.0 | 困难 |
| Q26 | 晶界扩散技术（GBD）如何减少重稀土用量？ | DyF3、晶界、30%~50% | 困难 |
| Q27 | 每辆纯电动汽车的永磁电机使用多少钕铁硼磁体？ | 1~2kg、0.2~0.4kg镝 | 中等 |
| Q28 | 直驱永磁风力发电机每台使用多少稀土磁材？ | 1吨、钕铁硼 | 中等 |
| Q29 | 2010年中国稀土出口限制引发了什么国际贸易争端？ | WTO、出口配额、2014年裁定 | 困难 |
| Q30 | 丰田开发的稀土回收工艺回收率是多少？ | 镍氢电池、96% | 中等 |

---

## 四、中医药现代化与AI辅助诊断

> 来源文档：`TCM_Modernization_AI_Diagnosis.pdf/docx/md`、`wiki_tcm.html`

| 编号 | 测试问题 | 期望关键词 | 难度 |
|------|----------|------------|------|
| Q31 | 中医证候概念为什么难以映射到 ICD-11？ | 整体性、动态性、ICD-11第26章 | 中等 |
| Q32 | 舌诊AI系统的典型流程包括哪些步骤？ | 舌体分割、颜色校正、特征提取、证候分类 | 中等 |
| Q33 | U-Net 在舌体分割任务上的 mIoU 可以达到多少？ | 0.92 | 中等 |
| Q34 | 中医舌色分为哪五类？各自主什么证？ | 淡红正常、淡白虚寒、红热、绛热盛、青紫瘀 | 中等 |
| Q35 | 舌象颜色校正有哪些方法？ | 色卡polynomial、Retinex、深度学习 | 困难 |
| Q36 | 脉诊仪的传感器类型有哪些？ | 压力、光电、声学 | 简单 |
| Q37 | 脉搏波信号的时域特征有哪些？ | h1主波、h2重搏前波、h3降中峡、h4重搏波 | 困难 |
| Q38 | 浮脉和沉脉在脉搏波特征上如何区分？ | 轻取即得、重按始得 | 困难 |
| Q39 | 中药知识图谱包含哪些实体和关系？ | 药材、性味归经、方剂-药材、证候-方剂 | 中等 |
| Q40 | ISO/TC 249 发布了哪些中医药国际标准？ | ISO 17218针灸针、ISO 18664重金属 | 困难 |

---

## 五、海水淡化技术

> 来源文档：`Desalination_Industry_Report.pdf/docx/md`、`wiki_desalination.html`

| 编号 | 测试问题 | 期望关键词 | 难度 |
|------|----------|------------|------|
| Q41 | 全球最大的海水淡化设施在哪里？日产能多少？ | 沙特Jubail、100万立方米 | 简单 |
| Q42 | SWRO 系统的操作压力和回收率分别是多少？ | 55~70 bar、40%~50% | 中等 |
| Q43 | PX 压力交换器的能量回收效率是多少？ | 大于96% | 中等 |
| Q44 | SWRO 采用 PX 后吨水能耗可以降低到多少？ | 2.5~3.5 kWh/立方米 | 中等 |
| Q45 | 超滤预处理可以将 SDI 降到多少？ | 2~3 | 中等 |
| Q46 | 低温 MED 的顶温控制在什么范围？为什么？ | 65~70度、减轻结垢腐蚀 | 中等 |
| Q47 | MED 的造水比 GOR 通常是多少？ | 8~10 | 中等 |
| Q48 | 浓盐水排放的环保措施有哪些？ | 扩散器、排前稀释、ZLD | 中等 |
| Q49 | MVR 蒸发结晶技术在浓盐水资源化中有什么优劣？ | 紧凑连续、能耗成本高 | 困难 |
| Q50 | SWRO 的产水成本中电费占比多少？ | 40%~50% | 中等 |

---

## 六、交叉领域问题（跨文档检索测试）

> 这些问题不直接对应单一文档，用于测试 RAG 系统的跨领域检索能力

| 编号 | 测试问题 | 期望命中的文档领域 | 难度 |
|------|----------|-------------------|------|
| Q51 | 哪些技术在工业物联网和精准农业中都有应用？ | IIoT传感器、边缘计算、IoT | 困难 |
| Q52 | 深度学习在哪些垂直行业中有具体应用？ | 舌诊AI、异常检测、NDVI、GNN | 困难 |
| Q53 | 稀土永磁材料在哪些新能源领域有应用？ | 电动汽车、风力发电 | 中等 |
| Q54 | 知识图谱技术在哪些行业中有应用？ | 中药组方推荐、农业知识图谱 | 困难 |
| Q55 | 环境影响评估在哪些行业中是关键议题？ | 浓盐水排放、稀土开采污染 | 中等 |

---

## 测试评分参考

| 命中率 | 评级 | 说明 |
|--------|------|------|
| ≥ 90% | 优秀 | RAG 系统检索效果出色 |
| 70%~89% | 良好 | 基本满足需求，个别困难问题可优化 |
| 50%~69% | 一般 | 查询重写或重排序可能需要调整 |
| < 50% | 需改进 | 检查切片策略、embedding 模型或入库流程 |

---

## 文件清单

### 从网络下载的文档（位于 `downloaded/` 目录）
| 文件名 | 行业领域 | 格式 | 大小 |
|--------|---------|------|------|
| wiki_industrial_iot.html | 工业物联网 | HTML | 353KB |
| wiki_edge_computing.html | 边缘计算 | HTML | 234KB |
| wiki_precision_agriculture.html | 精准农业 | HTML | 327KB |
| wiki_rare_earth.html | 稀土元素 | HTML | 1375KB |
| wiki_smart_grid.html | 智能电网 | HTML | 695KB |
| wiki_tcm.html | 中医药 | HTML | 1606KB |
| wiki_nuclear_power.html | 核电 | HTML | 552KB |
| wiki_pharma_industry.html | 制药 | HTML | 964KB |
| wiki_desalination.html | 海水淡化 | HTML | 909KB |
| wiki_aquaculture.html | 水产养殖 | HTML | 1012KB |
| wiki_bim.html | 建筑信息模型 | HTML | 689KB |

### 自动生成的文档（位于 `generated/` 目录）
| 文件名 | 行业领域 | 格式 | 大小 |
|--------|---------|------|------|
| IIoT_Edge_Computing_Guide.pdf | 工业物联网 | PDF | 133KB |
| IIoT_Edge_Computing_Guide.docx | 工业物联网 | Word | 39KB |
| IIoT_Edge_Computing_Guide.md | 工业物联网 | Markdown | 6KB |
| Precision_Agriculture_Whitepaper.pdf | 精准农业 | PDF | 136KB |
| Precision_Agriculture_Whitepaper.docx | 精准农业 | Word | 39KB |
| Precision_Agriculture_Whitepaper.md | 精准农业 | Markdown | 6KB |
| Rare_Earth_Supply_Chain_Report.pdf | 稀土供应链 | PDF | 138KB |
| Rare_Earth_Supply_Chain_Report.docx | 稀土供应链 | Word | 39KB |
| Rare_Earth_Supply_Chain_Report.md | 稀土供应链 | Markdown | 6KB |
| TCM_Modernization_AI_Diagnosis.pdf | 中医药AI | PDF | 122KB |
| TCM_Modernization_AI_Diagnosis.docx | 中医药AI | Word | 39KB |
| TCM_Modernization_AI_Diagnosis.md | 中医药AI | Markdown | 5KB |
| Desalination_Industry_Report.pdf | 海水淡化 | PDF | 131KB |
| Desalination_Industry_Report.docx | 海水淡化 | Word | 39KB |
| Desalination_Industry_Report.md | 海水淡化 | Markdown | 5KB |
