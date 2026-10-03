# 记忆领域知名文献与论文汇编

> 本文档为 **归忆（Remembrance-Lingering）** 项目的文献调研基础，汇总"记忆"主题下公认知名、被广泛引用的文章与论文。
> 覆盖范围以 **AI / LLM 智能体记忆** 为主线，并追溯 **记忆增强神经网络** 与 **认知神经科学** 的理论源头。
>
> - 维护方式：按"N. 分节"追加；新增条目请补齐标题、出处、链接与一句话说明。
> - 元数据可靠性：arXiv 编号经过检索核对；BibTeX 的 `author` 字段建议再用 Google Scholar / arXiv 导出二次校对。
> - 最后更新：2026-09-25

---

## 目录

1. [综述与全景图](#1-综述与全景图)
2. [里程碑：智能体记忆系统](#2-里程碑智能体记忆系统)
3. [参数化 / 隐空间 / 新型记忆架构](#3-参数化--隐空间--新型记忆架构)
4. [记忆增强神经网络的奠基之作](#4-记忆增强神经网络的奠基之作)
5. [长上下文 / 循环记忆架构](#5-长上下文--循环记忆架构)
6. [反思 / 经验型记忆](#6-反思--经验型记忆)
7. [认知科学与神经科学基石](#7-认知科学与神经科学基石)
8. [评测基准](#8-评测基准)
9. [持续更新的资源清单](#9-持续更新的资源清单)
10. [给"归忆"的选型建议](#10-给归忆的选型建议)
11. [推荐阅读路径](#11-推荐阅读路径)
12. [BibTeX 条目](#12-bibtex-条目)

---

## 1. 综述与全景图

| 论文 | 出处 / 编号 | 说明 |
| --- | --- | --- |
| **A Survey on the Memory Mechanism of LLM-based Agents** | [arXiv:2404.13501](https://arxiv.org/abs/2404.13501) | 人大团队。提出 Source / Forms / Operations 三要素框架，领域最常被引的入门综述。 |
| **Memory in the Age of AI Agents: A Survey** | [arXiv:2512.13564](https://arxiv.org/abs/2512.13564) | NUS、人大、复旦、北大、同济联合。以"形态–功能–动力学"三维框架整合 200+ 论文，主张用 Token-level / Parametric / Latent 取代长短记忆二分法。 |
| **Memory for Large Language Models** | arXiv:2607.25380（编号待核对） | 唐杰团队。以架构为中心的记忆分类法，从表征、更新动态、持久性三维度统一 KV Cache / Engram / MoE / Titans 等碎片化工作。 |
| **Knowledge Editing for Large Language Models: A Survey** | [arXiv:2310.16218](https://arxiv.org/abs/2310.16218) | 参数化记忆的写入与修改（模型编辑）路线综述。 |
| **Survey on Memory-Augmented Neural Networks: Cognitive Insights to AI Applications** | 未定编号，可检索标题 | 从认知记忆机制到 AI 应用的桥梁性综述。 |
| **Awesome-Context-Engineering → Memory Systems 章节** | [GitHub](https://github.com/welnailetter-bot/Awesome-Context-Engineering) | 持续更新的记忆系统论文/框架索引。 |

## 2. 里程碑：智能体记忆系统

工程落地最相关的一节。

| 工作 | 编号 | 核心思想 |
| --- | --- | --- |
| **MemGPT**（→ Letta） | [arXiv:2310.08560](https://arxiv.org/abs/2310.08560) | 把 LLM 当操作系统，虚拟内存式分层管理，模型自主换页上下文。 |
| **Generative Agents**（AI 小镇） | [arXiv:2304.03442](https://arxiv.org/abs/2304.03442) | memory stream + reflection + planning；检索按新近性/重要性/相关性加权打分。 |
| **MemoryBank / SiliconFriend** | [arXiv:2305.10250](https://arxiv.org/abs/2305.10250) | 引入艾宾浩斯遗忘曲线的记忆衰减与强化，长期 AI 伴侣场景。 |
| **A-MEM** | [arXiv:2502.12110](https://arxiv.org/abs/2502.12110) | 受 Zettelkasten 卡片盒启发，记忆自动建链、自组织成网，缓解结构僵化。 |
| **Mem0** | [arXiv:2504.19413](https://arxiv.org/abs/2504.19413) | 生产级可扩展长期记忆，提取–去重–更新–检索流水线，显著降低 token 开销。 |
| **MemOS** | [arXiv:2505.22101](https://arxiv.org/abs/2505.22101) / [arXiv:2507.03724](https://arxiv.org/abs/2507.03724) | 首个"记忆操作系统"，统一调度参数记忆 / 激活记忆 / 明文记忆。 |
| **HippoRAG / HippoRAG 2** | [arXiv:2405.14831](https://arxiv.org/abs/2405.14831) | 海马索引理论 + 知识图 + 个性化 PageRank，实现单次检索的多跳推理。 |
| **Zep / Graphiti** | [arXiv:2501.13956](https://arxiv.org/abs/2501.13956) | 时序知识图谱记忆层，支持双时态（事件时间 / 摄入时间）有效性。 |
| **MemoRAG** | [arXiv:2409.05591](https://arxiv.org/abs/2409.05591) | 用小模型承担全局记忆，提升长文档场景的 RAG 表现。 |
| **Think-in-Memory** | [arXiv:2311.08719](https://arxiv.org/abs/2311.08719) | 动态维护历史记忆，检索 + 后思考加工记忆。 |
| **RET-LLM** | Modarressi et al., 2023（编号待核对） | 面向 LLM 的通用读写记忆。 |
| **Memory-R1** | [arXiv:2508.19828](https://arxiv.org/abs/2508.19828) | 用强化学习让 Agent 自主学习记忆的增删改查策略。 |
| **Agentic Memory** | [arXiv:2601.01885](https://arxiv.org/abs/2601.01885)（编号待核对） | 统一长短期记忆，三阶段渐进 RL + 逐步 GRPO。 |

## 3. 参数化 / 隐空间 / 新型记忆架构

| 工作 | 编号 | 要点 |
| --- | --- | --- |
| **Titans** | [arXiv:2501.00663](https://arxiv.org/abs/2501.00663) | Google。测试时训练 + 神经长期记忆模块，突破上下文窗口瓶颈。 |
| **MemoryLLM** | [arXiv:2402.04624](https://arxiv.org/abs/2402.04624) | 在模型隐空间植入可自更新的记忆池。 |
| **M+** | [arXiv:2502.00592](https://arxiv.org/abs/2502.00592) | 扩展 MemoryLLM 的分级长短期隐空间记忆，CPU–GPU 分离部署。 |
| **Memory³** | [arXiv:2407.01178](https://arxiv.org/abs/2407.01178) | 鄂维南团队。提出 RAG 与模型参数之外的"第三种记忆"——显式记忆。 |
| **Larimar** | [arXiv:2403.11901](https://arxiv.org/abs/2403.11901) | 一次性写入、解耦式记忆增强。 |
| **MIRAS** | Google 记忆架构理论族，接续 Titans（编号待核对） | 从记忆视角重构序列模型的设计空间。 |

## 4. 记忆增强神经网络的奠基之作

| 工作 | 编号 / 出处 |
| --- | --- |
| **Memory Networks**（Weston et al., 2014） | [arXiv:1410.3916](https://arxiv.org/abs/1410.3916) |
| **End-to-End Memory Networks**（Sukhbaatar et al., 2015） | [arXiv:1503.08895](https://arxiv.org/abs/1503.08895) |
| **Neural Turing Machines**（Graves et al., 2014） | [arXiv:1410.5401](https://arxiv.org/abs/1410.5401) |
| **Differentiable Neural Computer**（Graves et al., 2016） | Nature 538:471–476 |
| **One-shot Learning with Memory-Augmented NN**（Santoro et al., 2016） | [arXiv:1605.06065](https://arxiv.org/abs/1605.06065) |
| **Hopfield Networks**（Hopfield, 1982） | PNAS 79(8):2554–2558，联想记忆的起点 |

## 5. 长上下文 / 循环记忆架构

| 工作 | 编号 |
| --- | --- |
| **RAG: Retrieval-Augmented Generation**（Lewis et al., 2020） | [arXiv:2005.11401](https://arxiv.org/abs/2005.11401) |
| **Self-RAG**（自反思检索） | [arXiv:2310.11511](https://arxiv.org/abs/2310.11511) |
| **Transformer-XL**（Dai et al., 2019） | [arXiv:1901.02860](https://arxiv.org/abs/1901.02860) |
| **Compressive Transformer**（Rae et al., 2019） | [arXiv:1911.05507](https://arxiv.org/abs/1911.05507) |
| **Recurrent Memory Transformer**（Bulatov et al., 2022） | [arXiv:2207.06881](https://arxiv.org/abs/2207.06881) |
| **Memorizing Transformers**（Wu et al., 2022） | [arXiv:2203.08913](https://arxiv.org/abs/2203.08913) |
| **LongMem**（Wang et al., 2023） | [arXiv:2306.07174](https://arxiv.org/abs/2306.07174) |
| **Mamba**（Gu & Dao, 2023） | [arXiv:2312.00752](https://arxiv.org/abs/2312.00752) |

## 6. 反思 / 经验型记忆

面向 Agent 自我进化的记忆形态。

| 工作 | 编号 | 记忆形态 |
| --- | --- | --- |
| **Reflexion** | [arXiv:2303.11366](https://arxiv.org/abs/2303.11366) | 把失败反馈写成语言反思，存入情景记忆供后续尝试读取 |
| **Voyager** | [arXiv:2305.16291](https://arxiv.org/abs/2305.16291) | 可增长的可执行技能库，按描述向量检索复用 |
| **ExpeL** | [arXiv:2308.10144](https://arxiv.org/abs/2308.10144) | 跨任务提炼可复用经验教训 |
| **Self-Refine** | [arXiv:2303.17651](https://arxiv.org/abs/2303.17651) | 生成–反馈–修订循环，无需额外训练 |

## 7. 认知科学与神经科学基石

记忆系统设计的理论源头，建议理解其"分层 / 巩固 / 索引"三大直觉。

| 理论 / 工作 | 年份 | 要点 |
| --- | --- | --- |
| **Ebbinghaus 遗忘曲线** | 1885 | 遗忘"先快后慢"，记忆保持随时间的指数式衰减 |
| **Miller《The Magical Number Seven, Plus or Minus Two》** | 1956 | 短时记忆容量约为 7±2 个组块 |
| **Atkinson & Shiffrin 多存储模型** | 1968 | 感觉记忆 / 短时记忆 / 长时记忆三层结构，LLM 分层记忆的直系祖先 |
| **Tulving 情景记忆 vs 语义记忆** | 1972 | 记忆按内容类型划分，后续扩展出自传体记忆 |
| **Baddeley & Hitch 工作记忆模型** | 1974 | 中央执行 + 语音环路 + 视觉空间画板；2000 年增补情景缓冲器 |
| **Craik & Lockhart 加工水平理论** | 1972 | 记忆强度取决于加工深度而非复述次数 |
| **McClelland, McNaughton & O'Reilly 互补学习系统** | 1995 | 海马快速学习 + 皮层慢速巩固，MemGPT 分层与 Mem0 抽取-固化均源于此 |
| **Teyler & DiScenna 海马记忆索引理论** | 1986 | 海马存储指向新皮层的"索引"，HippoRAG 的直接灵感 |
| **Squire 记忆系统分类** | 2004 | 陈述性 / 非陈述性记忆的神经基础划分 |

> 参考：`Psychological Review` 102(3):419–457 (1995)；`Behavioral Neuroscience` 100:147–152 (1986)。

## 8. 评测基准

| 基准 | 编号 | 测什么 |
| --- | --- | --- |
| **LoCoMo** | [arXiv:2402.17753](https://arxiv.org/abs/2402.17753) | 超长期对话记忆，平均 300 轮 / 9K token / 最多 35 个会话 |
| **LongMemEval** | [arXiv:2410.10813](https://arxiv.org/abs/2410.10813) | 5 项核心长期记忆能力，500 道高质量问题 |
| **MemoryAgentBench** | [arXiv:2507.05257](https://arxiv.org/abs/2507.05257) | 增量多轮交互下的 Agent 记忆评测（ICLR 2026） |
| **MemBench** | 编号待核对 | 多类型记忆能力的系统评测 |
| **HaluMem** | 编号待核对 | 记忆幻觉（存储与检索过程中的失真）评测 |

## 9. 持续更新的资源清单

| 资源 | 地址 | 内容 |
| --- | --- | --- |
| Awesome AI Memory | [GitHub](https://github.com/fomos-openai/awesome-ai-memory) | Agent 记忆的论文、基准、开源项目与生产平台索引 |
| LLM_Agent_Memory_Survey | [GitHub](https://github.com/nuster1128/LLM_Agent_Memory_Survey) | arXiv:2404.13501 官方仓库 |
| Awesome-Context-Engineering | [GitHub](https://github.com/welnailetter-bot/Awesome-Context-Engineering) | 记忆 / 上下文工程论文与实现合集 |
| Memory Papers | [memorypapers.org](https://memorypapers.org/) | 记忆论文聚合站 |
| 知乎《LLM-based Agent Memory 相关论文集锦》 | [知乎](https://zhuanlan.zhihu.com/p/676482106) | 中文精读导航 |

## 10. 给"归忆"的选型建议

按当前领域三条主线（记忆操作系统 / 参数化-隐空间记忆 / RL 记忆管理），建议：

1. **架构骨架**：MemGPT/Letta 的分层记忆 + MemOS 的记忆调度理念
2. **存储与检索**：HippoRAG（图索引）+ Zep/Graphiti（时序知识图谱），优于纯向量检索，利于多跳与时间推理
3. **记忆组织**：A-MEM 的自动建链卡片盒，解决"结构僵化、适应性不足"
4. **写入门控**：Mem0 的提取–去重–更新流水线，避免无差别写入导致噪声堆积
5. **遗忘与衰减**：MemoryBank 的遗忘曲线衰减 + Reflexion / ExpeL 的经验提炼
6. **验证标准**：以 LoCoMo、LongMemEval、MemoryAgentBench 为基准建立回归测试

> 三条主线对应的关键取舍：**明文记忆**易审计但上下文开销大；**参数化记忆**容量大但写入昂贵、易灾难性遗忘；**隐空间记忆**可端到端训练但可解释性弱。

## 11. 推荐阅读路径

- **先建地图**：2404.13501 → 2512.13564
- **再看系统**：MemGPT → Generative Agents → MemoryBank → A-MEM → Mem0 → MemOS
- **补理论根**：Atkinson-Shiffrin → Baddeley & Hitch → McClelland et al. 1995 → Teyler & DiScenna 1986
- **看新架构**：Titans → MemoryLLM → M+ → Memory³
- **看经典机制**：Memory Networks → Neural Turing Machine → Differentiable Neural Computer
- **最后对基准**：LoCoMo → LongMemEval → MemoryAgentBench

## 12. BibTeX 条目

> 仅收录有明确会议/期刊或 arXiv 编号的条目。请在投稿或正式引用前用 Google Scholar / arXiv 导出核对 `author` 与 `pages` 字段。

```bibtex
@article{zhang2024survey,
  title   = {A Survey on the Memory Mechanism of Large Language Model based Agents},
  author  = {Zhang, Zeyu and Bo, Xiaohe and Ma, Chen and Li, Rui and Chen, Xu and Dai, Quanyu and Zhu, Jieming and Dong, Zhenhua and Wen, Ji-Rong},
  journal = {arXiv preprint arXiv:2404.13501},
  year    = {2024}
}

@article{memoryage2025survey,
  title   = {Memory in the Age of AI Agents: A Survey},
  journal = {arXiv preprint arXiv:2512.13564},
  year    = {2025}
}

@article{packer2023memgpt,
  title   = {MemGPT: Towards LLMs as Operating Systems},
  author  = {Packer, Charles and Wooders, Sarah and Lin, Kevin and Fang, Vivian and Patil, Shishir G. and Stoica, Ion and Gonzalez, Joseph E.},
  journal = {arXiv preprint arXiv:2310.08560},
  year    = {2023}
}

@inproceedings{park2023generative,
  title     = {Generative Agents: Interactive Simulacra of Human Behavior},
  author    = {Park, Joon Sung and O'Brien, Joseph C. and Cai, Carrie J. and Morris, Meredith Ringel and Liang, Percy and Bernstein, Michael S.},
  booktitle = {Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology (UIST)},
  year      = {2023}
}

@article{zhong2023memorybank,
  title   = {MemoryBank: Enhancing Large Language Models with Long-Term Memory},
  author  = {Zhong, Wanjun and Guo, Lianghong and Gao, Qiqi and Ye, He and Wang, Yanlin},
  journal = {arXiv preprint arXiv:2305.10250},
  year    = {2023}
}

@article{xu2025amem,
  title   = {A-MEM: Agentic Memory for LLM Agents},
  author  = {Xu, Wujiang and Liang, Zujie and Mei, Kai and Gao, Hang and Tan, Juntao and Zhang, Yongfeng},
  journal = {arXiv preprint arXiv:2502.12110},
  year    = {2025}
}

@article{chhikara2025mem0,
  title   = {Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory},
  author  = {Chhikara, Prateek and Khant, Dev and Aryan, Saket and Singh, Taranjeet and Yadav, Deshraj},
  journal = {arXiv preprint arXiv:2504.19413},
  year    = {2025}
}

@article{li2025memos,
  title   = {MemOS: An Operating System for Memory-Augmented Generation of Large Language Models},
  journal = {arXiv preprint arXiv:2505.22101},
  year    = {2025}
}

@inproceedings{gutierrez2024hipporag,
  title     = {HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models},
  author    = {Guti{\'e}rrez, Bernal Jim{\'e}nez and Shu, Yiheng and Gu, Yu and Yasunaga, Michihiro and Su, Yu},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS)},
  year      = {2024}
}

@article{rasmussen2025zep,
  title   = {Zep: A Temporal Knowledge Graph Architecture for Agent Memory},
  author  = {Rasmussen, Preston and Paliychuk, Pavlo and Beauvais, Travis and Ryan, Jack and Chalef, Daniel},
  journal = {arXiv preprint arXiv:2501.13956},
  year    = {2025}
}

@article{behrouz2025titans,
  title   = {Titans: Learning to Memorize at Test Time},
  author  = {Behrouz, Ali and Zhong, Peilin and Mirrokni, Vahab},
  journal = {arXiv preprint arXiv:2501.00663},
  year    = {2025}
}

@article{wang2024memoryllm,
  title   = {MemoryLLM: Towards Self-Updatable Large Language Models},
  author  = {Wang, Yu and Chen, Xiusi and Shang, Jingbo and McAuley, Julian},
  journal = {arXiv preprint arXiv:2402.04624},
  year    = {2024}
}

@article{wang2025mplus,
  title   = {M+: Extending MemoryLLM with Scalable Long-Term Memory},
  author  = {Wang, Yu and Gao, Yifan and Chen, Xiusi and others},
  journal = {arXiv preprint arXiv:2502.00592},
  year    = {2025}
}

@article{weston2014memory,
  title   = {Memory Networks},
  author  = {Weston, Jason and Chopra, Sumit and Bordes, Antoine},
  journal = {arXiv preprint arXiv:1410.3916},
  year    = {2014}
}

@article{graves2014ntm,
  title   = {Neural Turing Machines},
  author  = {Graves, Alex and Wayne, Greg and Danihelka, Ivo},
  journal = {arXiv preprint arXiv:1410.5401},
  year    = {2014}
}

@article{graves2016dnc,
  title   = {Hybrid Computing Using a Neural Network with Dynamic External Memory},
  author  = {Graves, Alex and Wayne, Greg and Reynolds, Malcolm and others},
  journal = {Nature},
  volume  = {538},
  pages   = {471--476},
  year    = {2016}
}

@inproceedings{lewis2020rag,
  title     = {Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks},
  author    = {Lewis, Patrick and Perez, Ethan and Piktus, Aleksandra and others},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS)},
  year      = {2020}
}

@article{shinn2023reflexion,
  title   = {Reflexion: Language Agents with Verbal Reinforcement Learning},
  author  = {Shinn, Noah and Cassano, Federico and Gopinath, Ashwin and Narasimhan, Karthik and Yao, Shunyu},
  journal = {arXiv preprint arXiv:2303.11366},
  year    = {2023}
}

@article{wang2023voyager,
  title   = {Voyager: An Open-Ended Embodied Agent with Large Language Models},
  author  = {Wang, Guanzhi and Xie, Yuqi and Jiang, Yunfan and others},
  journal = {arXiv preprint arXiv:2305.16291},
  year    = {2023}
}

@inproceedings{zhao2023expel,
  title     = {ExpeL: LLM Agents Are Experiential Learners},
  author    = {Zhao, Andrew and Huang, Da and Xu, Quentin and others},
  booktitle = {Proceedings of the AAAI Conference on Artificial Intelligence},
  year      = {2024}
}

@article{maharana2024locomo,
  title   = {Evaluating Very Long-Term Conversational Memory of LLM Agents},
  author  = {Maharana, Adyasha and Lee, Dong-Ho and Tulyakov, Sergey and Bansal, Mohit and Barbieri, Francesco and Fang, Yuwei},
  journal = {arXiv preprint arXiv:2402.17753},
  year    = {2024}
}

@article{wu2024longmemeval,
  title   = {LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory},
  author  = {Wu, Di and Wang, Hongwei and Yu, Wenhao and others},
  journal = {arXiv preprint arXiv:2410.10813},
  year    = {2024}
}

@article{mcclelland1995cls,
  title   = {Why There Are Complementary Learning Systems in the Hippocampus and Neocortex},
  author  = {McClelland, James L. and McNaughton, Bruce L. and O'Reilly, Randall C.},
  journal = {Psychological Review},
  volume  = {102},
  number  = {3},
  pages   = {419--457},
  year    = {1995}
}

@article{teyler1986indexing,
  title   = {The Hippocampal Memory Indexing Theory},
  author  = {Teyler, Timothy J. and DiScenna, Pascal},
  journal = {Behavioral Neuroscience},
  volume  = {100},
  number  = {2},
  pages   = {147--152},
  year    = {1986}
}
```

---

*本文档仅为文献导航，所有结论请以原文为准。发现编号错误或遗漏，请直接修订对应条目。*
