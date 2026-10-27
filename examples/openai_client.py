"""Use the official OpenAI SDK against the gateway.

export OPENAI_BASE_URL=http://localhost:8000/v1 OPENAI_API_KEY=gw_...   # from bootstrap.sh
uv run python examples/openai_client.py
"""

from openai import OpenAI

client = OpenAI()  # reads OPENAI_BASE_URL and OPENAI_API_KEY from the environment

print("models:", [m.id for m in client.models.list().data])

resp = client.chat.completions.create(
    model="qwen-small",
    messages=[{"role": "user", "content": "In one sentence, what is an API gateway?"}],
)
print("reply:", resp.choices[0].message.content)
print("usage:", resp.usage)

print("stream: ", end="")
stream = client.chat.completions.create(
    model="qwen-small",
    messages=[{"role": "user", "content": "Count from 1 to 5."}],
    stream=True,
)
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
