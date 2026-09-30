import json

import pytest

from fantasy_ai import chat
from fantasy_ai.pending import PendingStore

ME = "+15550001111"


@pytest.fixture
def store(tmp_path):
    return PendingStore(tmp_path / "pending.json")


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "snapshot.json").write_text(json.dumps({"leagues": {"JVille 2022": {"week": 4, "roster": [
        {"name": "Adonai Mitchell", "pos": "WR"}, {"name": "Michael Pittman Jr.", "pos": "WR"},
        {"name": "Bijan Robinson", "pos": "RB"}]}}}))
    return d


def llm_says(**d):
    return lambda prompt: json.dumps({"intent": "other", "confidence": 0.9, **d})


def run(text, store, data, llm, sent=None):
    sent = sent if sent is not None else []
    return chat.respond(ME, text, None, store, llm,
                        lambda aid, c, s: sent.append(aid) or (0, f"#{aid} sent."), data_dir=data), sent


def test_exact_command_skips_llm(store, data):
    a = store.add("JVille 2022", "TRADE_PROPOSAL", "give X get Y", {}, {})
    boom = lambda p: (_ for _ in ()).throw(AssertionError("LLM called"))
    reply, sent = run(f"approve {a.id}", store, data, boom)
    assert sent == [a.id]


def test_vague_approval_asks_then_yes_sends(store, data):
    a = store.add("JVille 2022", "TRADE_PROPOSAL", "give Adams get Nacua", {}, {})
    reply, sent = run("yeah do the puka trade", store, data, llm_says(intent="approve", action_id=None))
    assert sent == [] and "Reply yes or no" in reply and "give Adams get Nacua" in reply
    reply, sent = run("yep", store, data, llm_says(intent="yes"))
    assert sent == [a.id]


def test_no_cancels_wait(store, data):
    store.add("JVille 2022", "WAIVER", "add X drop Y", {}, {})
    run("sure go ahead", store, data, llm_says(intent="approve", confidence=0.5))
    reply, sent = run("actually no", store, data, llm_says(intent="no"))
    assert sent == [] and "Nothing sent" in reply


def test_confident_named_approval_acts(store, data):
    store.add("JVille 2022", "WAIVER", "a", {}, {})
    b = store.add("JVille 2022", "WAIVER", "b", {}, {})
    reply, sent = run(f"go ahead with number {b.id}", store, data, llm_says(intent="approve", action_id=b.id, confidence=0.95))
    assert sent == [b.id]


def test_ambiguous_approval_lists_options(store, data):
    store.add("JVille 2022", "WAIVER", "add A", {}, {})
    store.add("JVille 2022", "WAIVER", "add B", {}, {})
    reply, sent = run("do it", store, data, llm_says(intent="approve"))
    assert sent == [] and "Which one?" in reply


def test_start_request_sets_override(store, data):
    reply, _ = run("start mitchell this week", store, data, llm_says(intent="start", player="mitchell"))
    prefs = json.loads((data / "preferences.json").read_text())
    assert prefs["JVille 2022"]["start_this_week"] == ["Adonai Mitchell"] and prefs["JVille 2022"]["override_week"] == 4
    assert "Adonai Mitchell will start" in reply


def test_never_drop_rule_and_unknown_player(store, data):
    run("never drop bijan", store, data, llm_says(intent="never_drop", player="Bijan"))
    assert json.loads((data / "preferences.json").read_text())["JVille 2022"]["never_drop"] == ["Bijan Robinson"]
    reply, _ = run("start zzz", store, data, llm_says(intent="start", player="zzz"))
    assert "couldn't tell which player" in reply


def test_chitchat_stays_quiet_and_question_answers(store, data):
    assert run("lol nice", store, data, llm_says(intent="other"))[0] is None
    calls = iter(['{"intent": "question", "confidence": 0.9}', "Pittman sat because of a tough matchup."])
    reply, _ = run("why'd you bench pittman", store, data, lambda p: next(calls))
    assert "Pittman" in reply


def test_bad_llm_output_is_other():
    assert chat.parse_intent("not json")["intent"] == "other"
    assert chat.parse_intent('{"intent": "delete_everything"}')["intent"] == "other"
