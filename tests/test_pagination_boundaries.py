import pytest

from scripts.pagination import PaginationBoundaryError, validate_boundaries


def test_parent_heading_and_table_conclusion_follow_their_own_section():
    bad = [
        "# 封面",
        "### 5、上一主题\n\n上一主题的正文。\n\n---\n\n## 二、新主题\n\n新主题的引言。",
        "### 1、第一项\n\n引出表格：\n\n| 维度 | 判断 |\n| --- | --- |\n| 需求 | 强 |",
        "当你用这张表做评估，结论就能用于配置资源。\n\n### 2、第二项\n\n第二项正文。",
    ]
    with pytest.raises(PaginationBoundaryError, match="标题"):
        validate_boundaries(bad)

    table_bad = [bad[0], "### 5、上一主题\n\n上一主题的正文。\n\n---",
                 "## 二、新主题\n\n新主题的引言。\n\n" + bad[2], bad[3]]
    with pytest.raises(PaginationBoundaryError, match="表格"):
        validate_boundaries(table_bad)

    correct = [table_bad[0], table_bad[1], table_bad[2] + "\n\n" + bad[3].split("\n\n", 1)[0],
               "### 2、第二项\n\n第二项正文。"]
    validate_boundaries(correct)


def test_parent_heading_can_start_a_standalone_intro_page():
    validate_boundaries(["# 封面", "## 二、主题\n\n与独立图片对应的主题导读。", "### 1、子主题\n\n正文。"])


def test_headings_inside_fenced_code_do_not_define_boundaries():
    validate_boundaries(["# 封面", "```markdown\n## 假标题\n```", "### 下一主题\n\n正文。"])
