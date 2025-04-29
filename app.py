from fastapi import FastAPI, Query, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import sqlite3
import openai
import toml
from datetime import datetime, timedelta
from markdown import markdown
from collections import defaultdict
import statistics
import zipfile
import tempfile
import shutil
import os
import asyncio

from history import load_conversations
from utils import time_group, human_readable_time, floor_hour
from llms import load_create_embeddings, search_similar, openai_api_cost, TYPE_CONVERSATION, TYPE_MESSAGE

DB_EMBEDDINGS = "data/embeddings.db"
DB_SETTINGS = "data/settings.db"
SECRETS_PATH = "data/secrets.toml"
CONVERSATIONS_PATH = "data/conversations.json"


# Initialize FastAPI app
app = FastAPI()
api_app = FastAPI(title="API")

conversations = load_conversations(CONVERSATIONS_PATH)

try:
    SECRETS = toml.load(SECRETS_PATH)
    OPENAI_ENABLED = True
except:
    print("-- No secrets found. Not able to access the OpenAI API.")
    OPENAI_ENABLED = False

if OPENAI_ENABLED:
    openai.organization = SECRETS["openai"]["organization"]
    openai.api_key = SECRETS["openai"]["api_key"]

    embeddings, embeddings_ids, embeddings_index = load_create_embeddings(DB_EMBEDDINGS, conversations)


# All conversation items
@api_app.get("/conversations")
def get_conversations():
    # Get favorites
    conn = connect_settings_db()
    cursor = conn.cursor()
    cursor.execute("SELECT conversation_id FROM favorites WHERE is_favorite = 1")
    rows = cursor.fetchall()
    favorite_ids = [row[0] for row in rows]
    conn.close()

    conversations_data = [{
        "group": time_group(conv.created),
        "id": conv.id, 
        "title": conv.title_str,
        "created": conv.created_str,
        "total_length": human_readable_time(conv.total_length, short=True),
        "is_favorite": conv.id in favorite_ids
        } for conv in conversations]
    return JSONResponse(content=conversations_data)


# All messages from a specific conversation by its ID
@api_app.get("/conversations/{conv_id}/messages")
def get_messages(conv_id: str):
    conversation = next((conv for conv in conversations if conv.id == conv_id), None)
    if not conversation:
        return JSONResponse(content={"error": "Invalid conversation ID"}, status_code=404)

    messages = []
    prev_created = None  # Keep track of the previous message's creation time
    for msg in conversation.messages:
        if not msg:
            continue

        # If there's a previous message and the time difference is 1 hour or more
        if prev_created and (msg.created - prev_created).total_seconds() >= 3600:
            delta = msg.created - prev_created
            time_str = human_readable_time(delta.total_seconds())            
            messages.append({
                "text": f"{time_str} passed", 
                "role": "internal"
                })

        messages.append({
            "text": markdown(msg.text),
            "role": msg.role, 
            "created": msg.created_str
        })

        # Update the previous creation time for the next iteration
        prev_created = msg.created

    response = {
        "conversation_id": conversation.id,
        "messages": messages
    }
    return JSONResponse(content=response)


@api_app.get("/activity")
def get_activity():
    activity_by_day = defaultdict(int)

    for conversation in conversations:
        for message in conversation.messages:
            day = message.created.date()
            activity_by_day[day] += 1 if message.author.role == 'user' else 0
    
    activity_by_day = {str(k): v for k, v in sorted(dict(activity_by_day).items())}

    return JSONResponse(content=activity_by_day)


@api_app.get("/activity/last24h")
def get_activity_last24h(
        role: str | None = Query(None, description="Filter by message role, e.g., 'user' or 'assistant'")
) -> JSONResponse:
    """
    Return last 24 h traffic in 1-h buckets.
    Example item: {"hour": "2025-04-29 09:00", "count": 5}
    """
    now = datetime.now()
    start = now - timedelta(hours=24)

    # build zero-filled buckets
    buckets = {
        (start + timedelta(hours=i)).strftime("%Y-%m-%d %H:00"): 0
        for i in range(25) # 25 buckets to cover the full 24h range inclusively
    }

    global conversations
    if not conversations:
         conversations = load_conversations()

    for conv in conversations:
        for msg in conv.messages:
            if msg.created < start:
                continue
            if role and msg.role != role:
                continue
            key = floor_hour(msg.created).strftime("%Y-%m-%d %H:00")
            if key in buckets:
                buckets[key] += 1

    sorted_buckets = sorted(buckets.items())

    return JSONResponse(content=[
        {"hour": k, "count": v} for k, v in sorted_buckets
    ])


@api_app.get("/statistics")
def get_statistics():
    # Calculate the min, max, and average lengths
    lengths = []
    for conv in conversations:
        lengths.append((conv.total_length, conv.id))
    # Sort conversations by length
    lengths.sort(reverse=True)

    if lengths:
        min_threshold_seconds = 1
        filtered_min_lengths = [l for l in lengths if l[0] >= min_threshold_seconds]
        min_length = human_readable_time(min(filtered_min_lengths)[0])
        max_length = human_readable_time(max(lengths)[0])
        avg_length = human_readable_time(statistics.mean([l[0] for l in lengths]))
    else:
        min_length = max_length = avg_length = "N/A"

    # Generate links for the top 3 longest conversations
    top_3_links = "".join([f"<a href='https://chat.openai.com/c/{l[1]}' target='_blank'>Chat {chr(65 + i)}</a><br/>" 
                   for i, l in enumerate(lengths[:3])])

    # Get the last chat message timestamp and backup age
    last_chat_timestamp = max(conv.created for conv in conversations)

    return JSONResponse(content={
        "Chat backup age": human_readable_time((datetime.now() - last_chat_timestamp).total_seconds()),
        "Last chat message": last_chat_timestamp.strftime('%Y-%m-%d'),
        "First chat message": min(conv.created for conv in conversations).strftime('%Y-%m-%d'),
        "Shortest conversation": min_length,
        "Longest conversation": max_length,
        "Average chat length": avg_length,
        "Top longest chats": top_3_links
    })


@api_app.get("/ai-cost")
def get_ai_cost():
    tokens_by_month = defaultdict(lambda: {'input': 0, 'output': 0})

    for conv in conversations:
        for msg in conv.messages:
            year_month = msg.created.strftime('%Y-%m')
            token_count = msg.count_tokens()

            if msg.role == "user":
                tokens_by_month[year_month]['input'] += openai_api_cost(msg.model_str, 
                                                                        input=token_count)
            else:
                tokens_by_month[year_month]['output'] += openai_api_cost(msg.model_str,
                                                                         output=token_count)

    # Make a list of dictionaries
    tokens_list = [
        {'month': month, 'input': int(data['input']), 'output': int(data['output'])}
        for month, data in sorted(tokens_by_month.items())
    ]

    return JSONResponse(content=tokens_list)


# Search conversations and messages
@api_app.get("/search")
def search_conversations(query: str = Query(..., min_length=3, description="Search query")):

    def add_search_result(search_results, result_type, conv, msg):
        search_results.append({
            "type": result_type,
            "id": conv.id,
            "title": conv.title_str,
            "text": markdown(msg.text),
            "role": msg.role,
            "created": conv.created_str if result_type == "conversation" else msg.created_str,
        })

    def find_conversation_by_id(conversations, id):
        return next((conv for conv in conversations if conv.id == id), None)

    def find_message_by_id(messages, id):
        return next((msg for msg in messages if msg.id == id), None)

    search_results = []

    if query.startswith('"') and query.endswith('"'):
        query = query[1:-1]
        query_exact = True
    else:
        query_exact = False

    if OPENAI_ENABLED and not query_exact:
        for _id in search_similar(query, embeddings_ids, embeddings_index):
            conv = find_conversation_by_id(conversations, embeddings[_id]["conv_id"])            
            if conv:
                result_type = embeddings[_id]["type"]
                if result_type == TYPE_CONVERSATION:
                    msg = conv.messages[0]
                else:
                    msg = find_message_by_id(conv.messages, _id)
                
                if msg:
                    add_search_result(search_results, result_type, conv, msg)
    else:
        for conv in conversations:
            query_lower = query.lower()
            if (conv.title or "").lower().find(query_lower) != -1:
                add_search_result(search_results, "conversation", conv, conv.messages[0])

            for msg in conv.messages:
                if msg and msg.text.lower().find(query_lower) != -1:
                    add_search_result(search_results, "message", conv, msg)

            if len(search_results) >= 10:
                break

    return JSONResponse(content=search_results)


# Upload conversation zip
@api_app.post("/upload_zip")
async def upload_zip(file: UploadFile = File(...)):
    if not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail="必须上传 .zip 文件")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, file.filename)
            # Save the uploaded file
            with open(zip_path, 'wb') as f:
                content = await file.read() # Read file content
                f.write(content)

            # Unzip asynchronously
            def sync_unzip(path, extract_to):
                with zipfile.ZipFile(path, 'r') as z:
                    names = z.namelist()
                    if 'conversations.json' not in names:
                        # Raise exception within the sync function to be caught by the caller
                        raise ValueError("zip 里找不到 conversations.json")
                    z.extract('conversations.json', extract_to)

            await asyncio.to_thread(sync_unzip, zip_path, tmpdir)

            extracted_path = os.path.join(tmpdir, 'conversations.json')

            # Move the new file (directly overwriting if it exists)
            shutil.move(extracted_path, CONVERSATIONS_PATH)

        # Reload conversations
        global conversations
        conversations = load_conversations(CONVERSATIONS_PATH)
        
        # Maybe update embeddings if enabled? For now, just reload conversations.
        # if OPENAI_ENABLED:
        #     global embeddings, embeddings_ids, embeddings_index
        #     embeddings, embeddings_ids, embeddings_index = load_create_embeddings(DB_EMBEDDINGS, conversations)

        return {"status": "ok", "detail": f"成功加载 {len(conversations)} 条对话。", "count": len(conversations)}

    except ValueError as ve: # Catch specific error from sync_unzip
         raise HTTPException(status_code=400, detail=str(ve))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="无效的 zip 文件")
    except Exception as e:
        # Log the error for debugging
        print(f"Error uploading/processing zip: {e}")
        raise HTTPException(status_code=500, detail=f"处理 zip 文件时出错: {e}")


# Toggle favorite status
@api_app.post("/toggle_favorite")
def toggle_favorite(conv_id: str):
    conn = connect_settings_db()
    cursor = conn.cursor()
    
    # Check if the conversation_id already exists in favorites
    cursor.execute("SELECT is_favorite FROM favorites WHERE conversation_id = ?", (conv_id,))
    row = cursor.fetchone()
    
    if row is None:
        # Insert new entry with is_favorite set to True
        cursor.execute("INSERT INTO favorites (conversation_id, is_favorite) VALUES (?, ?)", (conv_id, True))
        is_favorite = True
    else:
        # Toggle the is_favorite status
        is_favorite = not row[0]
        cursor.execute("UPDATE favorites SET is_favorite = ? WHERE conversation_id = ?", (is_favorite, conv_id))
    
    conn.commit()
    conn.close()
    
    return {"conversation_id": conv_id, "is_favorite": is_favorite}


def connect_settings_db():
    conn = sqlite3.connect(DB_SETTINGS)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            conversation_id TEXT PRIMARY KEY,
            is_favorite BOOLEAN
        );
    """)
    conn.commit()
    return conn


app.mount("/api", api_app)
app.mount("/", StaticFiles(directory="static", html=True), name="Static")
