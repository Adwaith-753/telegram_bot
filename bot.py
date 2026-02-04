import logging
import re
import datetime
import aiocron
import asyncio
import time
import pytz
from collections import defaultdict
from pymongo import MongoClient, errors
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackContext, filters
from dotenv import load_dotenv
import os
import nest_asyncio
import uuid
import random
from aiohttp import web
from telegram.ext import CallbackQueryHandler
import aiohttp

# Custom Timezone Formatter
class TimezoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # Use Indian Standard Time (IST)
        ist = pytz.timezone('Asia/Kolkata')
        ct = datetime.datetime.fromtimestamp(record.created, ist)
        if datefmt:
            s = ct.strftime(datefmt)
        else:
            try:
                s = ct.isoformat(timespec='milliseconds')
            except TypeError:
                s = ct.isoformat()
        return s
        
# Apply nest_asyncio for environments like Jupyter
nest_asyncio.apply()

# Load environment variables
load_dotenv()

# Configuration
TOKEN = os.getenv('TOKEN')
DB_URL = os.getenv('DB_URL')
SEARCH_GROUP_ID = int(os.getenv('SEARCH_GROUP_ID'))
STORAGE_GROUP_ID = int(os.getenv('STORAGE_GROUP_ID'))
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
PORT = int(os.getenv('PORT', 8088))  # Default to 8088 if not set
LIST_LIMIT = 10
delete_sessions = {}
# Logging Configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S %Z',  # Include timezone in the date format
    handlers=[
        logging.StreamHandler(),  # Console output
        logging.FileHandler('bot.log', encoding='utf-8')  # Log to file
    ]
)

# Get the root logger and apply the custom formatter
logger = logging.getLogger()
for handler in logger.handlers:
    handler.setFormatter(TimezoneFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S %Z'
    ))

# MongoDB Client Setup
def connect_mongo():
    retries = 5
    while retries > 0:
        try:
            client = MongoClient(DB_URL, serverSelectionTimeoutMS=5000)
            db = client['MoviesDB']
            collection = db['Movies']
            client.admin.command('ping')
            logging.info("MongoDB connection established.")
            return collection
        except errors.ServerSelectionTimeoutError as e:
            logging.error(f"MongoDB connection failed. Retrying... {e}")
            retries -= 1
            time.sleep(5)
    logging.critical("Failed to connect to MongoDB.")
    return None

collection = connect_mongo()
search_group_messages = []

# Helper function to sanitize Unicode text
def sanitize_unicode(text):
    """
    Sanitize Unicode text to remove invalid characters, such as surrogate pairs.
    """
    return text.encode('utf-8', 'ignore').decode('utf-8')

# Clean filename function
def clean_filename(filename):
    """Clean the uploaded filename by removing unnecessary tags and extracting relevant details."""
    # Remove text inside square brackets (like [CK], [1080p])
    filename = re.sub(r'\[.*?\]', '', filename)

    # Remove prefixes like @TamilMob_LinkZz and leading special characters
    filename = re.sub(r'^[@\W_]+', '', filename)  # Removes @, -, _, spaces at the start

    # Remove emojis and special characters
    filename = re.sub(r'[^\x00-\x7F]+', '', filename)

    # Replace underscores with spaces
    filename = re.sub(r'[_\s]+', ' ', filename).strip()

    # Remove unwanted tags
    pattern = r'(?i)(HDRip|10bit|x264|AAC\d*|MB|AMZN|WEB-DL|WEBRip|HEVC|x265|ESub|HQ|\.mkv|\.mp4|\.avi|\.mov|BluRay|DVDRip|720p|1080p|540p|SD|HD|CAM|DVDScr|R5|TS|Rip|BRRip|AC3|DualAudio|6CH|v\d+)(\W|$)'
    filename = re.sub(pattern, ' ', filename).strip()

    # Extract movie name, year, and language
    match = re.search(r'^(.*?)[\s_]*\(?(\d{4})\)?[\s_]*(Malayalam|Tamil|Hindi|Telugu|English)?', filename, re.IGNORECASE)

    if match:
        name = match.group(1).strip(" -._")  # Remove extra special characters
        year = match.group(2).strip() if match.group(2) else ""
        language = match.group(3).strip() if match.group(3) else ""

        # Format the cleaned name
        cleaned_name = f"{name} ({year}) {language}".strip()
        return re.sub(r'\s+', ' ', cleaned_name)  # Remove extra spaces

    # If no match is found, return the cleaned filename
    return filename.strip(" -._")

# Temporary storage for incomplete movie uploads
upload_sessions = defaultdict(lambda: {
    'files': [], 
    'image': None, 
    'movie_name': None,
    'awaiting_name_edit': False,
    'user_id': None
})

async def name_decision_handler(update: Update, context: CallbackContext):
    """Handle name editing decisions from inline buttons."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = upload_sessions.get(user_id)
    
    if not session:
        await query.message.reply_text("❌ Session expired. Please restart the upload process.")
        return

    if query.data == "edit_name":
        session['awaiting_name_edit'] = True
        await query.message.reply_text("✏️ Please send the new movie name:")

    elif query.data == "continue_name":
        session['awaiting_name_edit'] = False
        await query.message.reply_text(f"✅ Name confirmed: **{session['movie_name']}**", parse_mode="Markdown")
        
        # Check if we can save the movie now
        await check_and_save_movie(user_id, update, context)

async def text_handler(update: Update, context: CallbackContext):
    """Handle text messages for movie name editing - ONLY IN STORAGE GROUP."""
    # Only handle in storage group
    if update.effective_chat.id != STORAGE_GROUP_ID:
        return
    
    user_id = update.effective_user.id
    session = upload_sessions.get(user_id)
    
    if session and session['awaiting_name_edit']:
        new_name = sanitize_unicode(update.message.text.strip())
        session['movie_name'] = new_name
        session['awaiting_name_edit'] = False
        
        await update.message.reply_text(
            f"✅ Movie name updated to:\n\n**{new_name}**",
            parse_mode="Markdown"
        )
        
        # Check if we can save the movie now
        await check_and_save_movie(user_id, update, context)
        return

async def check_and_save_movie(user_id, update, context):
    """Check if all conditions are met and save the movie to database."""
    session = upload_sessions.get(user_id)
    
    if not session:
        return
    
    # Check if we have all required data
    if not (session['files'] and session['image'] and session['movie_name']):
        return
    
    # Create movie entry
    movie_id = str(uuid.uuid4())
    movie_entry = {
        'movie_id': movie_id,
        'name': session['movie_name'],  # This uses the EDITED name
        'media': {
            'documents': session['files'],
            'image': session['image']
        }
    }

    try:
        collection.insert_one(movie_entry)
        await update.message.reply_text(
            sanitize_unicode(f"✅ Successfully added movie: {session['movie_name']}")
        )

        # Send preview to search group
        if SEARCH_GROUP_ID:
            await send_preview_to_group(movie_entry, context)

        # Clear the session
        del upload_sessions[user_id]
        
    except Exception as e:
        logging.error(f"Database error: {str(e)}")
        await update.message.reply_text(
            sanitize_unicode("❌ Failed to add the movie. Please try again later.")
        )

async def send_preview_to_group(movie_entry, context):
    """Send the movie preview to the search group."""
    name = movie_entry.get('name', 'Unknown Movie')
    media = movie_entry.get('media', {})
    image_file_id = media.get('image', {}).get('file_id')
    deep_link = f"https://t.me/{context.bot.username}?start={movie_entry['movie_id']}"

    keyboard = [[InlineKeyboardButton("🎬 Download", url=deep_link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if image_file_id:
            await context.bot.send_photo(
                chat_id=SEARCH_GROUP_ID,
                photo=image_file_id,
                caption=sanitize_unicode(f"🎥 **{name}**"),
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=SEARCH_GROUP_ID,
                text=sanitize_unicode(f"🎥 **{name}**"),
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    except Exception as e:
        logging.error(f"Error sending preview for {sanitize_unicode(name)}: {sanitize_unicode(str(e))}")

async def add_movie(update: Update, context: CallbackContext):
    """Process movie uploads, cleaning filenames and managing sessions."""
    
    if update.effective_chat.id != STORAGE_GROUP_ID:
        return

    user_id = update.effective_user.id
    session = upload_sessions.setdefault(user_id, {
        'files': [], 
        'image': None, 
        'movie_name': None,
        'awaiting_name_edit': False
    })
    
    # Handle document (movie file) upload
    if update.message.document:
        file_info = update.message.document
        cleaned_name = clean_filename(file_info.file_name)
        
        session['files'].append({
            'file_id': file_info.file_id,
            'file_name': cleaned_name
        })
        
        # Set the movie name from the first file
        if not session['movie_name']:
            session['movie_name'] = cleaned_name
        
        # If user is admin, show edit options
        if user_id in ADMIN_IDS:
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Edit Name", callback_data="edit_name"),
                    InlineKeyboardButton("✅ Continue", callback_data="continue_name")
                ]
            ]
            await update.message.reply_text(
                sanitize_unicode(f"🎬 Detected Movie Name:\n\n**{cleaned_name}**\n\nEdit or continue?"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                sanitize_unicode(f"✅ File received: {cleaned_name}")
            )
            # For non-admin, check if we can save
            await check_and_save_movie(user_id, update, context)
    
    # Handle photo upload
    elif update.message.photo:
        image_info = update.message.photo
        largest_photo = max(image_info, key=lambda photo: photo.width * photo.height)
        
        session['image'] = {
            'file_id': largest_photo.file_id,
            'width': largest_photo.width,
            'height': largest_photo.height
        }
        
        await update.message.reply_text(sanitize_unicode("🖼 Image received"))
        
        # Check if we can save (for non-admin or when not editing)
        if user_id not in ADMIN_IDS or not session['awaiting_name_edit']:
            await check_and_save_movie(user_id, update, context)
               
async def search_movie(update: Update, context: CallbackContext):
    """
    Search for a movie in the database and send preview to group.
    Clicking the deep link opens the bot's PM, where the user can download files.
    """
    # Validate the command usage - ONLY IN SEARCH GROUP
    if update.effective_chat.id != SEARCH_GROUP_ID:
        return
    
    # Get the movie name from the user's message
    movie_name = sanitize_unicode(update.message.text.strip())
    if not movie_name:
        await update.message.reply_text(
            sanitize_unicode("🚨 Provide a movie name to search.")
        )
        return

    try:
        # Search for the movie in the database
        # Search by the EDITED name that was saved in DB
        regex_pattern = re.compile(re.escape(movie_name), re.IGNORECASE)
        results = list(collection.find({"name": {"$regex": regex_pattern}}).limit(10))

        if results:
            # Send preview messages for each movie result
            for result in results:
                name = result.get('name', 'Unknown Movie')
                media = result.get('media', {})
                image_file_id = media.get('image', {}).get('file_id')

                # Generate a direct deep link for bot PM with the movie ID
                deep_link = f"https://t.me/{context.bot.username}?start={result['movie_id']}"

                # Create an inline keyboard for the deep link
                keyboard = [
                    [InlineKeyboardButton(
                        "🎬 Download", 
                        url=deep_link
                    )],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                # Send movie preview with an image if available
                if image_file_id:
                    try:
                        await context.bot.send_photo(
                            chat_id=update.effective_chat.id,
                            photo=image_file_id,
                            caption=sanitize_unicode(f"🎥 **{name}**"),
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                    except Exception as e:
                        logging.error(
                            f"Error sending preview for {sanitize_unicode(name)}: {sanitize_unicode(str(e))}"
                        )
                else:
                    # If no image is available, send a text preview
                    await update.message.reply_text(
                        sanitize_unicode(f"🎥 **{name}**"),
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )
        else:
            # No movies found
            await update.message.reply_text(
                sanitize_unicode(f"🎬 No movies found for '{movie_name}'. Try a different search term.")
            )

    except Exception as e:
        logging.error(f"Search error: {sanitize_unicode(str(e))}")
        await update.message.reply_text(
            sanitize_unicode("❌ An unexpected error occurred. Please try again later.")
        )

# New handler for retrieving movie files
async def get_movie_files(update: Update, context: CallbackContext):
    """Send movie files to user via private message."""
    query = update.callback_query
    await query.answer()

    # Extract movie ID from callback data
    movie_id = query.data.split('_')[1]

    try:
        # Fetch movie details from database
        movie = collection.find_one({"movie_id": movie_id})
        
        if movie and 'media' in movie and 'documents' in movie['media']:
            # Send a message to the user
            await query.message.reply_text(
                sanitize_unicode(f"📤 Sending files for **{movie.get('name', 'Movie')}**"),
                parse_mode="Markdown"
            )

            # Send each document related to the movie
            for doc in movie['media']['documents']:
                document_file_id = doc.get('file_id')
                document_file_name = doc.get('file_name', 'movie_file')
                
                if document_file_id:
                    try:
                        await context.bot.send_document(
                            chat_id=query.from_user.id,
                            document=document_file_id,
                            caption=sanitize_unicode(f"🎥 {document_file_name}")
                        )
                    except Exception as e:
                        logging.error(f"Error sending document: {sanitize_unicode(str(e))}")
            
            # Optional: Send a completion message
            await query.message.reply_text(
                sanitize_unicode("✅ All files have been sent!")
            )
        else:
            await query.message.reply_text(
                sanitize_unicode("❌ No files found for this movie.")
            )
    
    except Exception as e:
        logging.error(f"Error fetching files for movie {movie_id}: {sanitize_unicode(str(e))}")
        await query.message.reply_text(
            sanitize_unicode("❌ An error occurred while fetching the movie files.")
        )

async def start(update: Update, context: CallbackContext):
    """Handle the /start command in bot PM and deep links."""
    
    user = update.effective_user
    chat = update.effective_chat

    # If /start has deep-link args (movie download)
    if context.args:
        movie_id = context.args[0]
        movie = collection.find_one({"movie_id": movie_id})

        if movie:
            name = movie.get('name', 'Unknown Movie')
            media = movie.get('media', {})
            image_file_id = media.get('image', {}).get('file_id')
            documents = media.get('documents', [])

            if image_file_id:
                await update.message.reply_photo(
                    photo=image_file_id,
                    caption=sanitize_unicode(f"🎥 **{name}**\n\n📁 Files: {len(documents)}"),
                    parse_mode="Markdown"
                )

            for doc in documents:
                if doc.get('file_id'):
                    await context.bot.send_document(
                        chat_id=chat.id,
                        document=doc['file_id']
                    )
        return

    # ---- NORMAL /START IN BOT PM ----
    bot_name = context.bot.first_name
    user_name = user.first_name or "User"

    text = (
        f"ʜᴇʏ {sanitize_unicode(user_name)} ,\n\n"
        f"Mʏ Nᴀᴍᴇ ɪs {sanitize_unicode(bot_name)} ,\n"
        f"ʏᴏᴜ ᴄᴀɴ ᴜsᴇ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ ɪ ᴡɪʟʟ ɢɪᴠᴇ "
        f"ᴍᴏᴠɪᴇs ᴏʀ sᴇʀɪᴇs ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ.!! 😍"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Add me to your chat 🤖", url=f"https://t.me/{context.bot.username}?startgroup=true")],

        # Button row 1
        [
            InlineKeyboardButton("💬 Commands", callback_data="btn_1"),
            InlineKeyboardButton("📦 Source", callback_data="btn_2"),
        ],

        # Button row 2
        [
            InlineKeyboardButton("📊 Status", callback_data="btn_3"),
            InlineKeyboardButton("❌ Close", callback_data="btn_4"),
        ],

        
    ]

    await update.message.reply_text(
        sanitize_unicode(text),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def list_movies(update: Update, context: CallbackContext):
    # 🔐 Admin check
    user_id = (
        update.effective_user.id
        if update.effective_user
        else update.callback_query.from_user.id
    )

    if user_id not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ Admin only command.")
        else:
            await update.callback_query.answer("❌ Admin only command.", show_alert=True)
        return

    # 📄 Get page number
    try:
        page = int(context.args[0]) if context.args else 1
    except ValueError:
        page = 1

    page = max(page, 1)

    total = collection.count_documents({})
    if total == 0:
        text = "❌ No movies found."
        if update.message:
            await update.message.reply_text(text)
        else:
            await update.callback_query.message.edit_text(text)
        return

    total_pages = max(1, (total + LIST_LIMIT - 1) // LIST_LIMIT)
    page = min(page, total_pages)

    skip = (page - 1) * LIST_LIMIT

    # 🔥 Newest movies first
    movies = list(
        collection.find()
        .sort("_id", -1)
        .skip(skip)
        .limit(LIST_LIMIT)
    )

    # 📝 Message text
    text = (
        f"🎬 **Movie List**\n\n"
        f"📦 Total Movies: **{total}**\n"
        f"📄 Page: **{page}/{total_pages}**\n\n"
    )

    for i, movie in enumerate(movies, start=1):
        text += f"**{i}.** {movie.get('name', 'Unknown')}\n"

    # ⬅➡ Pagination buttons
    nav_buttons = []

    if page > 1:
        nav_buttons.append(
            InlineKeyboardButton("⬅ Prev", callback_data=f"list_{page - 1}")
        )

    if page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton("➡ Next", callback_data=f"list_{page + 1}")
        )

    keyboard = []

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append(
        [InlineKeyboardButton("❌ Delete", callback_data=f"delete_page_{page}")]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await update.callback_query.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )


async def list_pagination_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split("_")[1])
    context.args = [str(page)]
    await list_movies(update, context)


async def delete_page_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split("_")[2])
    user_id = query.from_user.id

    # Send prompt and store it for later cleanup
    prompt_msg = await query.message.reply_text(
        "🗑 **Delete Movie**\n\n"
        "Send the **movie number (1–10)** OR **movie name** you want to delete.",
        parse_mode="Markdown"
    )

    delete_sessions[user_id] = {
        "page": page,
        "step": "ask",
        "list_message": query.message,
        "prompt_message": prompt_msg,
        "messages_to_delete": [prompt_msg.message_id]  # Track messages to delete
    }


async def delete_text_handler(update: Update, context: CallbackContext):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        return

    session = delete_sessions.get(user_id)
    if not session:
        return

    text = update.message.text.strip()

    # Store the user's input message ID for deletion
    session["messages_to_delete"].append(update.message.message_id)

    page = session["page"]
    skip = (page - 1) * LIST_LIMIT
    movies = list(collection.find().sort("_id", -1).skip(skip).limit(LIST_LIMIT))
    movie = None

    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(movies):
            movie = movies[idx]
    else:
        movie = collection.find_one({
            "name": {"$regex": re.escape(text), "$options": "i"}
        })

    if not movie:
        await update.message.reply_text("❌ Movie not found. Try again.")
        return

    session["movie"] = movie

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ No", callback_data="cancel_delete"),
        ]
    ]

    confirm_msg = await update.message.reply_text(
        f"⚠️ **Confirm Delete**\n\n"
        f"🎬 {movie['name']}\n\n"
        f"Do you want to delete this movie?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Store the confirmation message ID for deletion
    session["messages_to_delete"].append(confirm_msg.message_id)


async def delete_confirm_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # 🔐 Admin-only protection
    if user_id not in ADMIN_IDS:
        await query.answer("❌ Not authorized", show_alert=True)
        return

    session = delete_sessions.get(user_id)
    if not session or "movie" not in session:
        await query.answer("⚠️ Delete session expired", show_alert=True)
        return

    movie = session["movie"]

    if query.data == "confirm_delete":
        try:
            # 🗑 Delete movie
            collection.delete_one({"movie_id": movie["movie_id"]})

            # 🔄 Refresh list (SAFE WAY)
            page = session["page"]
            list_message = session["list_message"]

            total = collection.count_documents({})
            total_pages = max(1, (total + LIST_LIMIT - 1) // LIST_LIMIT)
            page = min(page, total_pages)

            skip = (page - 1) * LIST_LIMIT
            movies = list(
                collection.find()
                .sort("_id", -1)
                .skip(skip)
                .limit(LIST_LIMIT)
            )

            text = (
                f"🎬 **Movie List**\n\n"
                f"📦 Total Movies: **{total}**\n"
                f"📄 Page: **{page}/{total_pages}**\n\n"
            )

            for i, movie in enumerate(movies, start=1):
                text += f"**{i}.** {movie.get('name', 'Unknown')}\n"

            keyboard = []

            if page > 1:
                keyboard.append([
                    InlineKeyboardButton("⬅ Prev", callback_data=f"list_{page - 1}")
                ])

            if page < total_pages:
                keyboard.append([
                    InlineKeyboardButton("➡ Next", callback_data=f"list_{page + 1}")
                ])

            keyboard.append(
                [InlineKeyboardButton("❌ Delete", callback_data=f"delete_page_{page}")]
            )

            await list_message.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            # 🧹 Delete all tracked messages
            for msg_id in session.get("messages_to_delete", []):
                try:
                    await context.bot.delete_message(
                        chat_id=query.message.chat_id,
                        message_id=msg_id
                    )
                except Exception as e:
                    logging.error(f"Failed to delete message {msg_id}: {e}")

        except Exception as e:
            logging.error(f"Delete error: {e}")
            await query.message.reply_text("❌ Failed to delete movie.")

    else:
        # User cancelled - delete all tracked messages
        for msg_id in session.get("messages_to_delete", []):
            try:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=msg_id
                )
            except Exception as e:
                logging.error(f"Failed to delete message {msg_id}: {e}")

    # 🧹 Clear delete session
    delete_sessions.pop(user_id, None)


# Define the /id command handler
async def id_command(update: Update, context: CallbackContext):
    """Respond with the user's ID and the group ID."""
    user_id = update.effective_user.id  # Get the user's ID
    chat_id = update.effective_chat.id  # Get the group/chat ID

    # Construct the response
    response = (
        f"👤 Your ID: {user_id}\n"
        f"💬 Group ID: {chat_id}"
    )

    # Send the response back to the user
    await update.message.reply_text(response)


async def menu_buttons_cb(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_1":  # 💬 Commands
        text = (
            "💬 **Available Commands**\n\n"
            "🔹 /start – Start the bot\n"
            "🔹 /id – Get your ID & group ID\n"
            "🔹 /list – List movies (Admin only)\n"
            "🔹 Send movie name – Search movies (Search Group)\n"
        )

        await query.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Back", callback_data="back_home")]
            ])
        )

    elif query.data == "btn_2":  # 📦 Source
        await query.message.edit_text(
            "📦 **Source**\n\nThis bot is private.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Back", callback_data="back_home")]
            ])
        )

    elif query.data == "btn_3":  # 📊 Status
        total = collection.count_documents({})
        await query.message.edit_text(
            f"📊 **Bot Status**\n\n🎬 Movies: **{total}**\n✅ Bot is online",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Back", callback_data="back_home")]
            ])
        )

    elif query.data == "btn_4":  # ❌ Close
        await query.message.delete()

    elif query.data == "back_home":
        await query.message.edit_text(
            sanitize_unicode(
                f"ʜᴇʏ ,\n\n"
                f"Mʏ Nᴀᴍᴇ ɪs {context.bot.first_name} ,\n"
                f"ʏᴏᴜ ᴄᴀɴ ᴜsᴇ ᴍᴇ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ 😍"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "➕ Add me to your chat 🤖",
                    url=f"https://t.me/{context.bot.username}?startgroup=true"
                )],
                [
                    InlineKeyboardButton("💬 Commands", callback_data="btn_1"),
                    InlineKeyboardButton("📦 Source", callback_data="btn_2"),
                ],
                [
                    InlineKeyboardButton("📊 Status", callback_data="btn_3"),
                    InlineKeyboardButton("❌ Close", callback_data="btn_4"),
                ],
            ])
        )

async def start_web_server():
    """Start a web server for health checks."""
    async def handle_health(request):
        return web.Response(text="Bot is running")

    app = web.Application()
    app.router.add_get('/', handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web server running on port {PORT}")

async def keep_awake():
    """Ping the bot's hosting URL every 5 minutes to prevent sleeping."""
    url = "https://select-kitti-maxzues003-d3896a3f.koyeb.app/"
    max_retries = 5  # Maximum retries before giving up
    retry_delay = 10  # Start with a 10-second delay

    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        logging.info("✅ Ping successful: Bot is awake")
                        return  # Exit function on success
                    else:
                        logging.warning(f"⚠️ Ping failed (status {resp.status}), retrying...")

            except Exception as e:
                logging.error(f"❌ Error pinging self: {e}")

            # Exponential backoff before retrying
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)  # Max backoff time = 5 minutes

    logging.critical("🚨 Max retries reached. Bot might be inactive!")

# Schedule keep_awake() to run every 5 minutes
aiocron.crontab("*/5 * * * *", func=keep_awake)

async def main():
    """Main function to start the bot."""
    try:
        await start_web_server()

        application = ApplicationBuilder().token(TOKEN).build()

        # COMMAND HANDLERS
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("id", id_command))
        application.add_handler(CommandHandler("list", list_movies))

        # CALLBACKS
        application.add_handler(CallbackQueryHandler(name_decision_handler,pattern="^(edit_name|continue_name)$"))
        application.add_handler(CallbackQueryHandler(list_pagination_cb,pattern="^list_"))
        application.add_handler(CallbackQueryHandler(delete_page_cb,pattern="^delete_page_"))
        application.add_handler(CallbackQueryHandler(delete_confirm_cb,pattern="^(confirm_delete|cancel_delete)$"))
        application.add_handler(CallbackQueryHandler(menu_buttons_cb, pattern="^(btn_1|btn_2|btn_3|btn_4|back_home)$"))
        application.add_handler(CallbackQueryHandler(get_movie_files))

        # STORAGE GROUP
        application.add_handler(MessageHandler(filters.Document.ALL & filters.Chat(STORAGE_GROUP_ID),add_movie))
        application.add_handler(MessageHandler(filters.PHOTO & filters.Chat(STORAGE_GROUP_ID),add_movie))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Chat(STORAGE_GROUP_ID),text_handler))

        # SEARCH GROUP
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Chat(SEARCH_GROUP_ID),search_movie))

        # ADMIN DELETE INPUT (SAFE)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,delete_text_handler))

        await application.run_polling()

    except Exception as e:
        logging.error(f"Main loop error: {e}")

    finally:
        logging.info("Shutting down bot...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped manually.")
    except Exception as e:
        logging.error(f"Unexpected error in main block: {e}")
