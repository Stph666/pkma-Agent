"""生成阶段 B 的三类样例文档（PDF / DOCX / TXT）。

内容围绕本项目的技术主题，后续可直接用于检索评估集与报告截图。
用法：python scripts/make_samples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "data" / "samples"

PDF_TEXT = """检索增强生成技术说明

一、什么是检索增强生成
检索增强生成（Retrieval-Augmented Generation，简称 RAG）把信息检索与大语言模型生成结合起来。
系统先从个人知识库中检索与问题相关的片段，再把这些片段作为上下文交给模型，要求模型只依据上下文作答。
相比直接让模型凭参数记忆回答，RAG 的优势在于知识可更新、答案可追溯、幻觉显著减少。

二、核心处理链路
一条完整的链路包含五个环节：文档解析、文本切片、向量化、相似度检索、上下文组装与生成。
文档解析负责把 PDF、DOCX、TXT 等格式还原为纯文本，并保留页码等来源信息。
文本切片把长文档切成 300 到 800 字的片段，片段之间保留一定重叠，避免语义被硬切断。
向量化使用中文语义模型把片段映射为稠密向量，语义相近的片段在向量空间中距离更近。
检索阶段把用户问题也转成向量，按余弦相似度排序，取前若干条作为候选。
生成阶段把候选片段编号后放入提示词，要求模型在答案中用方括号编号标注引用来源。

三、切片的取舍
切片过短会丢失上下文，模型看到残缺的句子容易误解；切片过长会引入噪声，检索精度下降。
工程上通常以段落或标题为边界做递归切分，再按目标长度合并，重叠比例控制在百分之十到百分之二十。
本系统采用约五百字、八十字重叠的配置，并在句末标点处断开，避免把一句话切成两半。

四、引用与拒答
引用必须能追溯到具体文档与页码，否则回答无法核查。
服务端在返回答案的同时返回结构化的引用列表，前端不通过解析答案文本猜测来源。
当检索结果的相关性低于阈值时，系统不应强行作答，而应明确告知知识库中没有足够依据，
并展示最相关的片段供用户判断，是否改用通用知识回答由用户决定。

五、局限与改进方向
向量检索擅长语义匹配，但对精确的名称、编号、关系类问题不够敏感。
常见的改进是把知识图谱引入检索：先从问题中识别实体，再取该实体的一跳邻居作为补充上下文。
另一种改进是引入重排序模型，对初检结果做二次打分。
这些改进都会增加复杂度与延迟，需要结合评估集验证是否真的带来提升，而不是只凭感觉判断。
"""

DOCX_SECTIONS = [
    ("第一章 知识图谱基础", "知识图谱用节点表示实体，用边表示实体之间的关系，本质上是一张带标签的有向图。"),
    (
        "1.1 三元组",
        "知识图谱的最小单位是三元组，形如“头实体—关系—尾实体”，例如“检索增强生成—依赖—向量检索”。"
        "抽取三元组通常交给大语言模型完成，要求模型输出结构化的 JSON，再用校验规则过滤掉不合法结果。",
    ),
    (
        "1.2 实体规范化",
        "同一个实体可能有多种写法，例如英文缩写与中文全称。抽取后需要去空白、统一大小写、合并明显重复的形式，"
        "否则图谱中会出现大量孤立节点，关联查询的效果会明显变差。",
    ),
    (
        "第二章 图谱与向量检索的结合",
        "向量检索解决语义相似问题，图谱解决实体关联问题，两者互补。"
        "当问题中出现明确的实体名称时，可以先在图中定位该实体，取它的一跳邻居及其来源片段，"
        "再与向量召回结果按片段编号去重合并，共同作为上下文。这种做法在跨段落的关系型问题上通常有收益。",
    ),
]

TXT_TEXT = """个人知识管理笔记

一、为什么要做知识管理
资料收藏得多不等于掌握了。真正有用的是能在需要的时候找到它，并且知道它出自哪里。
所以我给自己的知识库定了三条规矩：来源可查、更新及时、检索要快。

二、增量更新
文档会改版，笔记会补充。每次全量重建索引既慢又浪费，正确做法是按文档更新：
先按内容哈希判断文件是否真的变了，变了就删除该文档的旧切片与旧向量，再用同一条文档记录重新解析入库。
删除文档时，向量库、切片表、图谱关系必须一起清理，否则检索会返回已经不存在的来源。

三、标签与分类
分类用固定集合，避免标签越加越乱。我给自己的分类是：课程资料、技术文档、研究论文、工作资料、个人笔记、其他。
标签允许自由生成，但数量控制在三到六个，用来描述主题、方法与场景。

四、检索习惯
提问要具体。与其问“这个项目怎么样”，不如问“切片长度对召回有什么影响”。
问题越具体，向量检索越容易命中真正相关的段落。跨文档比较类的问题，先分别检索再对比，效果通常好于一次性提问。

五、维护节奏
每周花十分钟做三件事：删掉重复文档、补上缺失标签、检查失败记录。
知识库不是一次性工程，维护成本低才能长期用下去。
"""


def build_pdf(path: Path) -> None:
    import pymupdf

    # 按章节拆成两页，便于演示“按页码溯源”
    split_at = PDF_TEXT.index("四、引用与拒答")
    pages = [PDF_TEXT[:split_at].strip(), PDF_TEXT[split_at:].strip()]

    document = pymupdf.open()
    for content in pages:
        page = document.new_page()
        rect = pymupdf.Rect(56, 56, 540, 786)
        overflow = page.insert_textbox(rect, content, fontname="china-s", fontsize=10.5, lineheight=1.5)
        if overflow < 0:
            raise RuntimeError("PDF 文本超出单页，请拆分内容")
    document.set_metadata({"title": "检索增强生成技术说明", "author": "课程设计"})
    document.save(str(path))
    document.close()


def build_docx(path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("知识图谱与实体关系抽取", level=1)
    for title, body in DOCX_SECTIONS:
        if title.startswith("第"):
            document.add_heading(title, level=2)
        else:
            document.add_heading(title, level=3)
        document.add_paragraph(body)

    table = document.add_table(rows=4, cols=3)
    rows = [
        ("实体类型", "示例", "说明"),
        ("技术", "检索增强生成", "系统与方法"),
        ("组件", "向量数据库", "存储与检索向量"),
        ("指标", "Hit@5", "前五条是否命中正确来源"),
    ]
    for row_index, row_values in enumerate(rows):
        for col_index, value in enumerate(row_values):
            table.cell(row_index, col_index).text = value
    document.save(str(path))


def build_txt(path: Path) -> None:
    path.write_text(TXT_TEXT, encoding="utf-8")


def main() -> int:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    pdf_path = SAMPLES / "检索增强生成技术说明.pdf"
    docx_path = SAMPLES / "知识图谱与实体关系.docx"
    txt_path = SAMPLES / "个人知识管理笔记.txt"

    build_pdf(pdf_path)
    build_docx(docx_path)
    build_txt(txt_path)

    for path in (pdf_path, docx_path, txt_path):
        print(f"{path.name}  {path.stat().st_size / 1024:.1f} KB")
    print(f"输出目录：{SAMPLES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
