"""One cooperative deadline for a full inference chain, below the browser wait budget."""
import asyncio
from contextlib import asynccontextmanager

INFERENCE_SECONDS = {'agent': 130.0, 'diagnosis': 100.0}


class InferenceBudgetExceeded(TimeoutError):
    pass


@asynccontextmanager
async def inference_budget(kind):
    deadline = asyncio.timeout(INFERENCE_SECONDS[kind])
    try:
        async with deadline:
            yield
    except TimeoutError as error:
        if deadline.expired():
            raise InferenceBudgetExceeded('Inference chain deadline reached') from error
        raise
