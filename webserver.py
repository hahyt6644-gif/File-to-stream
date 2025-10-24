# webserver.py (FULL AND FINAL CODE with Lifespan)

import math
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pyrogram.file_id import FileId
from pyrogram import raw, Client

from config import Config
from bot import bot, initialize_clients, multi_clients, work_loads, get_readable_file_size

# --- Lifespan Manager (Starts and Stops the Bot with the Server) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    print("Web server is starting up...")
    print("Starting bot...")
    await bot.start()
    print("Main bot started.")
    print("Initializing clients...")
    await initialize_clients(bot)
    print("All clients initialized. Application startup complete.")
    yield
    # Application shutdown
    print("Web server is shutting down...")
    if bot.is_initialized:
        await bot.stop()
    print("Bot stopped.")


# --- FastAPI App ---
app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

class_cache = {}

# --- Health Check Route ---
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """Health check route for Render that handles GET and HEAD"""
    return {"status": "ok", "message": "Server is healthy and running!"}


# --- ByteStreamer Class for Streaming Logic ---
class ByteStreamer:
    def __init__(self, client: Client):
        self.client = client

    @staticmethod
    async def get_location(file_id: FileId):
        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    async def yield_file(self, file_id: FileId, index: int, offset: int, first_part_cut: int, last_part_cut: int, part_count: int, chunk_size: int):
        client = self.client
        work_loads[index] += 1
        
        media_session = client.media_sessions.get(file_id.dc_id)
        if media_session is None:
            if file_id.dc_id != await client.storage.dc_id():
                media_session = await client.create_media_session(file_id.dc_id)
            else:
                media_session = client.session
            client.media_sessions[file_id.dc_id] = media_session
        
        location = await self.get_location(file_id)
        current_part = 1
        
        try:
            while current_part <= part_count:
                r = await media_session.send(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size)
                )
                if isinstance(r, raw.types.upload.File):
                    chunk = r.bytes
                    if not chunk: break
                    
                    if part_count == 1: yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1: yield chunk[first_part_cut:]
                    elif current_part == part_count: yield chunk[:last_part_cut]
                    else: yield chunk
                    
                    current_part += 1
                    offset += chunk_size
                else: break
        finally:
            work_loads[index] -= 1


# --- API Routes ---
@app.get("/show/{unique_id}")
async def show_file_page(request: Request, unique_id: str):
    try:
        main_bot = multi_clients.get(0)
        if not main_bot: raise HTTPException(503, "Bot not initialized yet")

        async for msg in main_bot.get_chat_history(Config.LOG_CHANNEL, limit=2000):
            if msg.text and f"Unique ID: `{unique_id}`" in msg.text:
                storage_msg_id = int(msg.text.split("Storage Msg ID: `")[1].split("`")[0])
                
                file_msg = await main_bot.get_messages(Config.STORAGE_CHANNEL, storage_msg_id)
                media = file_msg.document or file_msg.video or file_msg.audio
                if not media: raise HTTPException(404, "File media not found in message.")

                file_name = media.file_name
                file_size = get_readable_file_size(media.file_size)
                mime_type = media.mime_type or "application/octet-stream"
                
                is_video = mime_type.startswith("video/")
                is_audio = mime_type.startswith("audio/")
                
                dl_link = f"{Config.BASE_URL}/dl/{storage_msg_id}/{file_name}"
                
                context = {
                    "request": request, "file_name": file_name, "file_size": file_size,
                    "is_media": is_video or is_audio, "direct_dl_link": dl_link,
                    "mx_player_link": f"intent:{dl_link}#Intent;action=android.intent.action.VIEW;type={mime_type};end",
                    "vlc_player_link": f"vlc://{dl_link}"
                }
                return templates.TemplateResponse("show.html", context)
                
        raise HTTPException(status_code=404, detail="Link expired or invalid.")
    except Exception as e:
        print(f"Error in /show: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/dl/{msg_id}/{file_name}")
async def stream_handler(request: Request, msg_id: int, file_name: str):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get, default=0)
    client = multi_clients.get(index)
    if not client: raise HTTPException(503, "No available clients to stream.")

    if client in class_cache:
        tg_connect = class_cache[client]
    else:
        tg_connect = ByteStreamer(client)
        class_cache[client] = tg_connect

    try:
        message = await client.get_messages(Config.STORAGE_CHANNEL, msg_id)
        if not (message.video or message.document or message.audio) or message.empty:
            raise FileNotFoundError

        media = message.document or message.video or message.audio
        file_id = FileId.decode(media.file_id)
        file_size = media.file_size
        
        from_bytes = 0
        until_bytes = file_size - 1
        if range_header:
            from_bytes_str, until_bytes_str = range_header.replace("bytes=", "").split("-")
            from_bytes = int(from_bytes_str)
            if until_bytes_str:
                until_bytes = int(until_bytes_str)

        if (until_bytes >= file_size) or (from_bytes < 0):
            raise HTTPException(416, "Range not satisfiable")

        chunk_size = 1024 * 1024
        req_length = until_bytes - from_bytes + 1
        offset = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = (until_bytes % chunk_size) + 1
        part_count = math.ceil(req_length / chunk_size)

        body = tg_connect.yield_file(file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size)

        return StreamingResponse(
            content=body,
            status_code=206 if range_header else 200,
            headers={
                "Content-Type": media.mime_type or "application/octet-stream",
                "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
                "Content-Length": str(req_length),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{media.file_name}"'
            },
        )
    except FileNotFoundError:
        raise HTTPException(404, "File not found on Telegram.")
    except Exception as e:
        print(f"Error in /dl: {traceback.format_exc()}")
        raise HTTPException(500, "Internal streaming error.")
