import argparse
import asyncio
import json

from medibot.core.schemas import ChatMessage, MedibotRequest
from medibot.orchestrator.workflow import MedibotWorkflow


async def run_smoke(message: str, session_id: str | None = None) -> dict:
    workflow = MedibotWorkflow()
    response = await workflow.handle(
        MedibotRequest(
            session_id=session_id,
            messages=[ChatMessage(role="user", content=message)],
        )
    )
    return response.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one MediBot P0 smoke request.")
    parser.add_argument("--message", required=True)
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()
    result = asyncio.run(run_smoke(args.message, args.session_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
