# 时效数据定义

specimen 为字符串标本号，保留前导零。project 对应版本化项目契约。department 是原始科室名称。received 与 released 必须带偏移，存储转换为UTC。urgent为0或1，变成明确布尔值。时间缺失哨兵不计成零周转。

版本以接收时刻匹配[start,end)；无匹配与多匹配均隔离。常规标本按上海时区周一至周五09:00–12:00、13:00–17:00核算，calendar.json中的节假日除外。急诊使用接收至签发的自然分钟。approved暂停先合并，再与可计时区间求交；常规非工作段不重复扣除，未批准暂停不扣除。

时效分钟使用Decimal，超出上限才记超时，等于上限达标。宽限为0至60分钟。签发早于接收、无偏移、超过31天等全部隔离。空文件、仅表头和全空行分别返回EMPTY_FILE、HEADER_ONLY、BLANK_ROWS。

同一标本完整字段相同只保留最早来源，其余隔离；同一标本字段冲突全部隔离。批次摘要包含原始字节与三个规则文件摘要及规则版本，参数变更产生新批次。P50/P95使用最近秩，空科室没有分位值。

## 交接暂停与责任归属

pauses.json按标本号索引，每条交接暂停含 start、end、approved，可用 from_department/to_department 或 departments/parties 声明实际交接科室，handover_id 为稳定编号。已批准的交接先与可计时窗口求交并合并；合并等待只从标本净用时扣除一次，未批准交接只保留证据、不扣减。

责任分摊按互不重叠的最小等待段进行：每段由覆盖它的实际交接科室均分，单科室覆盖的小段由该科室承担，份额之和精确等于标本暂停扣除。净用时归标本记录科室（申请科室）。响应中的 responsibility 给出三组并排数据：specimens（标本净用时、暂停、毛用时、责任合计）、departments（责任净用时、承担等待、责任用时）、rows（逐标本逐科室拆分）；handovers 保留原始交接记录及有效段。

恒等式：责任净用时合计=标本净用时；分摊等待合计=暂停扣除；责任用时合计=净用时+暂停扣除。任一不成立即 balanced=false 且 issues 给出原因，现行责任结论不切换，复核返回 RESPONSIBILITY_UNBALANCED 或 RESPONSIBILITY_STALE。
