from efb_telegram_master.solitaire import (
    SolitaireCandidate,
    build_command_text,
    has_solitaire_header,
    parse_solitaire,
    resolve_solitaire_action,
)


def candidate(uid, master_id, text):
    return SolitaireCandidate(
        slave_message_id=uid,
        canonical_master_msg_id=master_id,
        editable_master_msg_id=master_id,
        text=text,
    )


def solitaire(*items, header="#接龙", note="下午5.30左右到"):
    lines = [header, note, ""]
    lines.extend(f"{i}. {item}" for i, item in enumerate(items, start=1))
    return "\n".join(lines)


def test_solitaire_header_must_be_first_non_empty_line():
    assert has_solitaire_header("\n#接龙\n1. A")
    assert has_solitaire_header("#接龍\n1. A")
    assert not has_solitaire_header("hello\n#接龙\n1. A")
    assert not has_solitaire_header("我们来 #接龙 吧\n1. A")


def test_parse_wechat_solitaire_format():
    parsed = parse_solitaire(solitaire("A材料学院水果美食团购", "abc 火龙果4个"))

    assert parsed is not None
    assert parsed.layout == "."
    assert [i.index for i in parsed.items] == [1, 2]
    assert parsed.items[0].content == "A材料学院水果美食团购"


def test_parse_rejects_non_continuous_indices():
    assert parse_solitaire("#接龙\n\n1. A\n3. C") is None


def test_continuation_edits_when_prefix_is_inherited():
    old = solitaire("A", "B", "C")
    new = solitaire("A", "B", "C", "D")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "EDIT"
    assert plan.canonical_master_msg_id == "1.10"
    assert plan.reason == "continuation"


def test_short_continuation_rejects_prefix_change():
    old = solitaire("A", "B", "C")
    new = solitaire("A", "B edited", "C", "D")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "SEND"


def test_long_continuation_allows_one_prefix_change():
    old = solitaire("A", "B", "C", "D", "E")
    new = solitaire("A", "B edited", "C", "D", "E", "F")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "EDIT"


def test_correction_allows_two_item_changes_for_long_solitaire():
    old = solitaire("A", "B", "C", "D", "E", "F")
    new = solitaire("A", "B edited", "C", "D edited", "E", "F")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "EDIT"
    assert plan.reason == "correction"


def test_short_correction_is_rejected():
    old = solitaire("A", "B", "C", "D")
    new = solitaire("A", "B edited", "C", "D")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "SEND"


def test_single_deletion_edits_when_remaining_items_match():
    old = solitaire("茄子QieZhi", "茄子QieZhi 2", "估计", "66")
    new = solitaire("茄子QieZhi", "估计", "66")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "EDIT"
    assert plan.reason == "deletion"


def test_deletion_plus_item_change_is_rejected():
    old = solitaire("A", "B", "C", "D")
    new = solitaire("A", "C edited", "D")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "SEND"


def test_item_spacing_and_punctuation_changes_count_as_edits():
    old = solitaire("A", "西瓜果切2盒，西北", "C", "D", "E")
    new = solitaire("A", "西瓜果切 2盒 西北", "C", "D", "E")

    plan = resolve_solitaire_action(new, "new", [candidate("old", "1.10", old)])

    assert plan.action_type == "EDIT"
    assert plan.reason == "correction"


def test_ambiguous_candidates_fall_back_to_send():
    old = solitaire("A", "B", "C")
    new = solitaire("A", "B", "C", "D")

    plan = resolve_solitaire_action(
        new,
        "new",
        [candidate("old-a", "1.10", old), candidate("old-b", "1.11", old)],
    )

    assert plan.action_type == "SEND"
    assert plan.reason == "ambiguous_match"


def test_command_appends_to_base_candidate():
    base = solitaire("A", "B")

    plan = resolve_solitaire_action("cmd 张三", "new", [candidate("old", "1.10", base)], command="cmd")

    assert plan.action_type == "EDIT"
    assert plan.replacement_text == base + "\n3. 张三"


def test_command_without_candidate_is_dropped():
    plan = resolve_solitaire_action("cmd 张三", "new", [], command="cmd")

    assert plan.action_type == "DROP"


def test_build_command_text_revalidates_result():
    assert build_command_text(solitaire("A"), "张三") == solitaire("A") + "\n2. 张三"
    assert build_command_text("not solitaire", "张三") is None
    assert build_command_text(solitaire("A"), "   ") is None
