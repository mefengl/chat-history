# ChatGPT history

UI for navigating and organizing OpenAI's ChatGPT conversations.

**Important**: This project is 100% unaffiliated with OpenAI.

## Features

- See activity graph and useful statistics
- Quickly browse and open the chats
- Search chats (semantic and "strict")
- List of favorite chats
- Open conversations on the ChatGPT site

![Screenshot](static/screenshot.png)

## Setup

Currently can only be installed locally. Requires Python 3.10+

1. [Export ChatGPT history](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data)
2. Unzip the download, place `conversations.json` in the `data` folder
3. `uv sync`           # or make install
4. `uv run uvicorn app:app --reload --port 8080`   # or make run
5. Open http://127.0.0.1:8080 in your browser
6. *Optional* - copy `secrets.template.toml` to `data/secrets.toml` and update OpenAI API key, then restart the server. First run will take a while to create embeddings. 10MB JSON: ~30 min, ~$0.10 cost.
