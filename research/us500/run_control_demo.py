"""Offline walkthrough of the US500 paper buttons and the controlled runner."""

import argparse
import copy
import json
from pathlib import Path

from example_session import session
from us500_control import PaperControl
from us500_engine import save_new
from us500_runner import replay


class SimulatedOwner:
    """Fictional owner and callback updates; no Telegram transport or token."""

    def __init__(self, control):
        self.control = control
        self.sequence = 0
        self.transcript = []

    def click(self, data):
        self.sequence += 1
        update = {
            "update_id": self.sequence,
            "callback_query": {
                "id": "synthetic-query-" + str(self.sequence),
                "from": {"id": 11, "is_bot": False},
                "message": {
                    "message_id": 1,
                    "date": 1791744840,
                    "chat": {"id": 11, "type": "private"},
                },
                "data": data,
            },
        }
        answers = self.control.handle_update(
            update, now_ms=1791744840000 + self.sequence
        )
        for answer in answers:
            if answer["method"] == "sendMessage":
                payload = answer["payload"]
                self.transcript.append(
                    {
                        "text": payload["text"],
                        "buttons": [
                            [button["text"] for button in row]
                            for row in payload.get("reply_markup", {}).get(
                                "inline_keyboard", []
                            )
                        ],
                    }
                )
        return answers

    def enable(self):
        answer = self.click("u5:enable")
        confirm = next(
            r["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            for r in answer
            if r["method"] == "sendMessage"
        )
        self.click(confirm)


def run(directory):
    root = Path(directory)
    root.mkdir(exist_ok=False)
    control = PaperControl(root / "control.sqlite3", owner_user_id=11, owner_chat_id=11)
    owner = SimulatedOwner(control)
    data = session()
    prefix = copy.deepcopy(data)
    prefix["frames"] = prefix["frames"][:3]
    database = root / "paper.sqlite3"
    owner.click("u5:status")
    disabled = replay(data, database, control=control)
    owner.enable()
    initial = replay(prefix, database, control=control)
    owner.click("u5:pause")
    before = database.read_bytes()
    paused = replay(data, database, control=control)
    unchanged = database.read_bytes() == before
    if not unchanged:
        raise RuntimeError("PAUSE_CHANGED_LEDGER")
    owner.enable()
    final = replay(data, database, control=control)
    restarted = replay(data, database, control=control)
    if final != restarted:
        raise RuntimeError("CONTROLLED_RESTART_DIVERGED")
    owner.click("u5:pause")
    summary = {
        "schema": "us500-paper-control-walkthrough-v1",
        "synthetic": True,
        "telegram_network_used": False,
        "actual_orders": 0,
        "initial_gate": disabled["status"],
        "pause_gate": paused["status"],
        "position_at_pause": initial["final_state"]["position_quantity"],
        "ledger_unchanged_while_paused": unchanged,
        "restart_equal": True,
        "final_cash_usd": final["final_state"]["cash_usd"],
        "final_position_quantity": final["residual_position_quantity"],
        "final_reserved_margin_usd": final["final_state"]["reserved_margin_usd"],
        "paper_enabled_at_end": control.snapshot()["paper_enabled"],
        "production_control_available": False,
    }
    save_new(root / "SUMMARY.json", summary)
    save_new(
        root / "BUTTON_TRANSCRIPT.json",
        {"synthetic": True, "messages": owner.transcript},
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().directory), indent=2))
