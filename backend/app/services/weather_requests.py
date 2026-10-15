"""Share in-flight weather work; callers receive independent result dictionaries."""
import asyncio
import copy


_pending = {}


async def share_weather_request(key, fetch):
    scoped_key = (asyncio.get_running_loop(), key)
    task = _pending.get(scoped_key)
    if task is None:
        task = asyncio.create_task(fetch())
        _pending[scoped_key] = task

        def finished(done):
            if _pending.get(scoped_key) is done:
                _pending.pop(scoped_key, None)
            # Consume exceptions even if every HTTP caller disconnected.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
    # Leaving one page must not cancel another page's shared weather request.
    return copy.deepcopy(await asyncio.shield(task))
