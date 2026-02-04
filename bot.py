import logging
import re
import datetime
import aiocron
import asyncio
import time
import pytz
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
from telegram.ext import ContextTypes
from bson import ObjectId


# Custom Timezone Formatter
class TimezoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
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
        
nest_asyncio.apply()
load_dotenv()

# Logging Configuration (Setup before loading config)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S %Z',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)

logger = logging.getLogger()
for handler in logger.handlers:
    handler.setFormatter(TimezoneFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S %Z'
    ))

# Configuration
TOKEN = os.getenv('TOKEN')
DB_URL = os.getenv('DB_URL')
SEARCH_GROUP_ID = int(os.getenv('SEARCH_GROUP_ID'))
STORAGE_GROUP_ID = int(os.getenv('STORAGE_GROUP_ID'))
PORT = int(os.getenv('PORT', 8088))
PAGE_SIZE = 10

# ✅ IMPROVED ADMIN IDS PARSING
def load_admin_ids():
    """Load and validate admin IDs from environment."""
    try:
        raw_ids = os.getenv("ADMIN_IDS", "")
        
        if not raw_ids:
            logging.warning("⚠️ ADMIN_IDS not set in .env file!")
            return set()
        
        logging.info(f"📋 Raw ADMIN_IDS from .env: '{raw_ids}'")
        
        # Clean and parse IDs
        admin_ids = set()
        for id_str in raw_ids.split(","):
            cleaned_id = id_str.strip()
            if cleaned_id and cleaned_id.isdigit():
                admin_ids.add(int(cleaned_id))
                logging.info(f"   ✅ Added admin ID: {cleaned_id}")
            else:
                logging.warning(f"   ⚠️ Skipping invalid admin ID: '{id_str}'")
        
        logging.info(f"🔑 Successfully loaded {len(admin_ids)} admin(s)")
        
        return admin_ids
        
    except Exception as e:
        logging.error(f"❌ Error parsing ADMIN_IDS: {e}")
        return set()

ADMIN_IDS = load_admin_ids()

# Verify at least one admin exists
if not ADMIN_IDS:
    logging.critical("🚨 No valid admin IDs found! Bot will have limited functionality.")
else:
    logging.info(f"👑 Admin IDs loaded: {sorted(ADMIN_IDS)}")

# Helper function to check admin status
def is_admin(user_id: int) -> bool:
    """Check if user is admin."""
    return user_id in ADMIN_IDS

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
    return text.encode('utf-8', 'ignore').decode('utf-8')

# Clean filename function
def clean_filename(filename):
    """Clean the uploaded filename by removing unnecessary tags."""
    filename = re.sub(r'\[.*?\]', '', filename)
    filename = re.sub(r'^[@\W_]+', '', filename)
    filename = re.sub(r'[^\x00-\x7F]+', '', filename)
    filename = re.sub(r'[_\s]+', ' ', filename).strip()
    
    pattern = r'(?i)(HDRip|10bit|x264|AAC\d*|MB|AMZN|WEB-DL|WEBRip|HEVC|x265|ESub|HQ|\.mkv|\.mp4|\.avi|\.mov|BluRay|DVDRip|720p|1080p|540p|SD|HD|CAM|DVDScr|R5|TS|Rip|BRRip|AC3|DualAudio|6CH|v\d+)(\W|$)'
    filename = re.sub(pattern, ' ', filename).strip()
    
    match = re.search(r'^(.*?)[\s_]*\(?(\d{4})\)?[\s_]*(Malayalam|Tamil|Hindi|Telugu|English)?', filename, re.IGNORECASE)
    
    if match:
        name = match.group(1).strip(" -._")
        year = match.group(2).strip() if match.group(2) else ""
        language = match.group(3).strip() if match.group(3) else ""
        cleaned_name = f"{name} ({year}) {language}".strip()
        return re.sub(r'\s+', ' ', cleaned_name)
    
    return filename.strip(" -._")

# Helper function to extract language from filename
def extract_language_from_filename(filename):
    """Extract language from filename."""
    filename_lower = filename.lower()
    
    # Check for language patterns
    languages = {
        'hindi': 'Hindi',
        'tamil': 'Tamil', 
        'malayalam': 'Malayalam',
        'telugu': 'Telugu',
        'english': 'English',
        'eng': 'English',
        'hin': 'Hindi',
        'tam': 'Tamil',
        'mal': 'Malayalam',
        'tel': 'Telugu'
    }
    
    for lang_key, lang_name in languages.items():
        if lang_key in filename_lower:
            return lang_name
    
    # Check parentheses format: Movie Name (2023) Hindi
    match = re.search(r'\((?:19|20)\d{2}\)\s*([A-Za-z]+)', filename)
    if match:
        possible_lang = match.group(1).lower()
        for lang_key, lang_name in languages.items():
            if lang_key == possible_lang:
                return lang_name
    
    return 'Unknown'

# Temporary storage for incomplete movie uploads
upload_sessions = {}

delete_sessions = {}

# ============================
# LANGUAGE SELECTION HANDLERS
# ============================

async def show_language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: str):
    """Show language selection buttons to user."""
    
    movie = collection.find_one({"movie_id": movie_id})
    
    if not movie:
        await update.message.reply_text("❌ Movie not found.")
        return
    
    name = movie.get('name', 'Unknown Movie')
    media = movie.get('media', {})
    documents = media.get('documents', [])
    
    # Get unique languages
    languages = set()
    for doc in documents:
        lang = doc.get('language', 'Unknown')
        if lang != 'Unknown':
            languages.add(lang)
    
    # If no languages detected, show "All Files" only
    if not languages:
        keyboard = [[InlineKeyboardButton("📦 All Files", callback_data=f"lang_{movie_id}_all")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        image_file_id = media.get('image', {}).get('file_id')
        if image_file_id:
            await update.message.reply_photo(
                photo=image_file_id,
                caption=f"**{name}**\n\nNo language tags detected. Showing all files:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                f"**{name}**\n\nNo language tags detected. Showing all files:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        return
    
    # Create language buttons (2 per row) - NO COUNT
    keyboard = []
    row = []
    
    for lang in sorted(languages):
        button = InlineKeyboardButton(
            f"{lang}",
            callback_data=f"lang_{movie_id}_{lang}"
        )
        row.append(button)
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    # Add "All Files" button - NO COUNT
    keyboard.append([InlineKeyboardButton(
        "📦 All Files",
        callback_data=f"lang_{movie_id}_all"
    )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send movie poster with language selection
    image_file_id = media.get('image', {}).get('file_id')
    
    if image_file_id:
        await update.message.reply_photo(
            photo=image_file_id,
            caption=f"**{name}**\n\nSelect language:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            f"**{name}**\n\nSelect language:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

async def send_language_files(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                            movie_id: str, language: str):
    """Send files of specific language to user."""
    
    # Handle callback query (if clicked from buttons)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        message_id = query.message.message_id
    else:
        # Handle deep link
        chat_id = update.effective_chat.id
        message_id = None
    
    movie = collection.find_one({"movie_id": movie_id})
    
    if not movie:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Movie not found."
        )
        return
    
    name = movie.get('name', 'Unknown Movie')
    media = movie.get('media', {})
    documents = media.get('documents', [])
    
    # Filter documents by language
    if language != "all":
        filtered_docs = [doc for doc in documents if doc.get('language') == language]
    else:
        filtered_docs = documents
    
    if not filtered_docs:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ No {language} files found for this movie."
        )
        return
    
    # Send starting message
    if language == "all":
        lang_text = "all files"
    else:
        lang_text = f"{language} files"
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📤 Sending **{lang_text}** for:\n\n**{name}**",
        parse_mode="Markdown"
    )
    
    # Send each document
    sent_count = 0
    for doc in filtered_docs:
        document_file_id = doc.get('file_id')
        document_file_name = doc.get('file_name', 'movie_file')
        doc_language = doc.get('language', 'Unknown')
        
        if document_file_id:
            try:
                # Add language emoji to caption
                lang_emoji = {
                    'Hindi': '🇮🇳',
                    'Tamil': '🇮🇳',
                    'Malayalam': '🇮🇳',
                    'Telugu': '🇮🇳',
                    'English': '🇺🇸'
                }.get(doc_language, '🎬')
                
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=document_file_id,
                    caption=f"{lang_emoji} {document_file_name}"
                )
                sent_count += 1
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logging.error(f"Error sending document: {sanitize_unicode(str(e))}")
    
    # Send completion message
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Successfully sent **{sent_count} file(s)**!"
    )
    
    # Delete the language selection message (if it was a callback)
    if update.callback_query and message_id:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=message_id
            )
        except:
            pass  # Ignore if can't delete

async def language_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle language selection callback queries."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("lang_"):
        # Format: lang_{movie_id}_{language}
        parts = data.split("_")
        if len(parts) >= 3:
            movie_id = parts[1]
            language = "_".join(parts[2:])  # In case language has underscores
            await send_language_files(update, context, movie_id, language)

# ============================
# UPLOAD HANDLERS
# ============================

async def name_decision_handler(update: Update, context: CallbackContext):
    """Handle Edit / Continue button actions."""

    query = update.callback_query
    await query.answer()  # prevent Telegram resend

    user_id = query.from_user.id

    # 🚫 RESTRICT TO ADMINS ONLY
    if not is_admin(user_id):
        await query.message.reply_text(
            "❌ Only admins can edit movie names."
        )
        return

    session = upload_sessions.get(user_id)

    # 🔕 Silent ignore if already completed
    if not session or session.get("saved"):
        return

    # ✏️ EDIT NAME FLOW
    if query.data == "edit_name":
        # Stop if already saved
        if session.get("saved"):
            await query.message.reply_text(
                "⚠️ Movie already saved. Upload a new one to edit."
            )
            return

        session["awaiting_name_edit"] = True

        await query.message.reply_text(
            "✏️ Please send the new movie name:"
        )
        return

    # ✅ CONTINUE FLOW
    elif query.data == "continue_name":

        # 🚫 Prevent double execution
        if session.get("saved"):
            return

        session["awaiting_name_edit"] = False
        session["name_confirmed"] = True

        # 🔒 Disable buttons immediately
        await query.message.edit_reply_markup(reply_markup=None)

        await query.message.reply_text(
            f"✅ Name confirmed:\n\n**{session['movie_name']}**",
            parse_mode="Markdown"
        )

        # ✅ Save ONLY if everything is ready
        if (
            session.get("files")
            and session.get("image")
            and session.get("movie_name")
        ):
            await check_and_save_movie(user_id, update, context)


async def text_handler(update: Update, context: CallbackContext):
    """Handle movie name input after Edit button."""
    
    # Only process in storage group
    if update.effective_chat.id != STORAGE_GROUP_ID:
        return

    # Make sure we have a message
    if not update.message:
        return

    user_id = update.effective_user.id
    
    # 🚫 RESTRICT TO ADMINS ONLY
    if not is_admin(user_id):
        return

    session = upload_sessions.get(user_id)

    # Only accept text when waiting for edit
    if not session or not session.get('awaiting_name_edit'):
        return

    new_name = sanitize_unicode(update.message.text.strip())

    if not new_name:
        await update.message.reply_text("❌ Movie name cannot be empty.")
        return

    # Save edited name
    session['movie_name'] = new_name
    session['awaiting_name_edit'] = False
    session['name_confirmed'] = True   # 🔥 IMPORTANT

    await update.message.reply_text(
        f"✅ Movie name updated to:\n\n**{new_name}**",
        parse_mode="Markdown"
    )

    # Save movie if everything is ready
    if (
        session['files']
        and session['image']
        and session['movie_name']
        and session['name_confirmed']
    ):
        await check_and_save_movie(user_id, update, context)

async def check_and_save_movie(user_id, update, context):
    """Check if all conditions are met and save the movie to database."""

    session = upload_sessions.get(user_id)
    if not session:
        return

    # 🚫 Stop if already processing or saved
    if session.get("processing") or session.get("saved"):
        return

    # 🚫 Name must be confirmed
    if not session.get("name_confirmed"):
        return

    # 🚫 Ensure required data exists
    if not (
        session.get("files")
        and session.get("image")
        and session.get("movie_name")
    ):
        return

    # 🔒 HARD LOCK (prevents double execution)
    session["processing"] = True
    session["saved"] = True

    movie_id = str(uuid.uuid4())
    movie_entry = {
        "movie_id": movie_id,
        "name": session["movie_name"],
        "media": {
            "documents": session["files"],
            "image": session["image"]
        }
    }

    try:
        # ✅ Insert into DB
        collection.insert_one(movie_entry)

        success_text = sanitize_unicode(
            f"✅ Successfully added movie:\n\n🎬 **{session['movie_name']}**"
        )

        # ✅ Respond ONLY ONCE
        if update.callback_query:
            await update.callback_query.answer()  # 🔥 important
            await update.callback_query.message.reply_text(
                success_text, parse_mode="Markdown"
            )
        elif update.message:
            await update.message.reply_text(
                success_text, parse_mode="Markdown"
            )

        # ✅ Send preview to search group
        if SEARCH_GROUP_ID:
            await send_preview_to_group(movie_entry, context)

        session["completed_at"] = time.time()

    except Exception as e:
        # 🔓 Rollback locks
        session["saved"] = False
        session["processing"] = False

        logging.error(f"Database error: {e}")

        error_text = sanitize_unicode(
            "❌ Failed to add the movie. Please try again later."
        )

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(error_text)
        elif update.message:
            await update.message.reply_text(error_text)

async def send_preview_to_group(movie_entry, context):
    """Send the movie preview to the search group."""
    name = movie_entry.get('name', 'Unknown Movie')
    media = movie_entry.get('media', {})
    image_file_id = media.get('image', {}).get('file_id')
    deep_link = f"https://t.me/{context.bot.username}?start=select_{movie_entry['movie_id']}"

    # SIMPLE DOWNLOAD BUTTON (no count)
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
    """Process movie uploads. Ask Edit/Continue ONLY when image is uploaded."""

    # Only allow in storage group
    if update.effective_chat.id != STORAGE_GROUP_ID:
        return

    user_id = update.effective_user.id


    # Create or get upload session
    session = upload_sessions.setdefault(user_id, {
        'files': [],
        'image': None,
        'movie_name': None,
        'awaiting_name_edit': False,
        'name_confirmed': False,
        'saved': False,
        'user_id': user_id
    })

    # =========================
    # 📁 HANDLE DOCUMENT UPLOAD
    # =========================
    if update.message.document:

        # 🔄 RESET SESSION if previous movie already saved
        if session.get("saved"):
            upload_sessions[user_id] = {
                'files': [],
                'image': None,
                'movie_name': None,
                'awaiting_name_edit': False,
                'name_confirmed': False,
                'saved': False,
                'user_id': user_id
            }
            session = upload_sessions[user_id]

        file_info = update.message.document
        cleaned_name = clean_filename(file_info.file_name)

        # Extract language
        language = extract_language_from_filename(cleaned_name)

        session['files'].append({
            'file_id': file_info.file_id,
            'file_name': cleaned_name,
            'language': language
        })

        # Set movie name from first file only
        if not session['movie_name']:
            session['movie_name'] = cleaned_name

        await update.message.reply_text(
            sanitize_unicode(f"➕ File added: {cleaned_name}")
        )
        return

    # ======================
    # 🖼️ HANDLE IMAGE UPLOAD
    # ======================
    if update.message.photo:

        # ❌ Block image before files
        if not session['files']:
            await update.message.reply_text(
                "📂 Please upload movie files before the image."
            )
            return

        largest_photo = max(
            update.message.photo,
            key=lambda p: p.width * p.height
        )

        session['image'] = {
            'file_id': largest_photo.file_id,
            'width': largest_photo.width,
            'height': largest_photo.height
        }

        await update.message.reply_text(
            sanitize_unicode("🖼 Image received")
        )

        # 🔥 ASK EDIT / CONTINUE ONLY ONCE
        if not session['name_confirmed']:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✏️ Edit Name", callback_data="edit_name"),
                    InlineKeyboardButton("✅ Continue", callback_data="continue_name")
                ]
            ])

            await update.message.reply_text(
                sanitize_unicode(
                    f"🎬 Detected Movie Name:\n\n**{session['movie_name']}**\n\nEdit or continue?"
                ),
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            # Name already confirmed → save safely
            if (
                session["files"]
                and session["image"]
                and session["movie_name"]
                and session["name_confirmed"]
                and not session["saved"]
            ):
                await check_and_save_movie(user_id, update, context)

# ============================
# SEARCH HANDLER
# ============================

async def search_movie(update: Update, context: CallbackContext):
    """
    Search for a movie in the database and send preview to group.
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
        regex_pattern = re.compile(re.escape(movie_name), re.IGNORECASE)
        results = list(collection.find({"name": {"$regex": regex_pattern}}).limit(10))

        if results:
            # Send preview messages for each movie result
            for result in results:
                name = result.get('name', 'Unknown Movie')
                media = result.get('media', {})
                image_file_id = media.get('image', {}).get('file_id')
                
                # Create callback data with movie_id
                deep_link = f"https://t.me/{context.bot.username}?start=select_{result['movie_id']}"
                
                # SIMPLE DOWNLOAD BUTTON (no count)
                keyboard = [[InlineKeyboardButton("🎬 Download", url=deep_link)]]
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

# ============================
# START HANDLER
# ============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_name = context.bot.first_name
    args = context.args

     # 🔒 Allow /start ONLY in private chat
    if update.effective_chat.type != "private":
        return
    
    # 🔹 Language selection handling
    if args and args[0].startswith("select_"):
        movie_id = args[0].replace("select_", "")
        await show_language_selection(update, context, movie_id)
        return
    
    # 🔹 Language-specific download handling
    if args and args[0].startswith("lang_"):
        # Format: lang_{movie_id}_{language}
        parts = args[0].split("_")
        if len(parts) >= 3:
            movie_id = parts[1]
            language = "_".join(parts[2:])  # In case language has underscores
            await send_language_files(update, context, movie_id, language)
            return
    
    # 🔹 Deep link movie handling (old style - for backward compatibility)
    if args:
        movie_id = args[0]
        movie = collection.find_one({"movie_id": movie_id})

        if movie:
            name = movie.get('name', 'Unknown Movie')
            media = movie.get('media', {})
            image_file_id = media.get('image', {}).get('file_id')
            documents = media.get('documents', [])

            if image_file_id:
                await update.message.reply_photo(
                    photo=image_file_id,
                    caption=f"🎥 **{name}**",
                    parse_mode="Markdown"
                )

            for doc in documents:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=doc["file_id"]
                )
            return

    # 🔹 Home menu
    text = (
        f"ʜᴇʏ {sanitize_unicode(user.first_name)} ,\n"
        f"Mʏ Nᴀᴍᴇ ɪs {sanitize_unicode(bot_name)}, ʏᴏᴜ ᴄᴀɴ ᴜsᴇ ᴍᴇ ɪɴ ʏᴏᴜʀ "
        f"ɢʀᴏᴜᴘ ɪ ᴡɪʟʟ ɢɪᴠᴇ ᴍᴏᴠɪᴇs ᴏʀ sᴇʀɪᴇs ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘ.!! 😍"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "➕ Add Me To Your Chat",
            url=f"https://t.me/{context.bot.username}?startgroup=true"
        )],
        [
            InlineKeyboardButton("💬 Commands", callback_data="menu_comments"),
            InlineKeyboardButton("📦 Source", callback_data="menu_source")
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="menu_status"),
            InlineKeyboardButton("❌ Close", callback_data="menu_close")
        ]
    ])

    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.callback_query.message.edit_text(text, reply_markup=keyboard)

# ============================
# MENU HANDLERS
# ============================

async def menu_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📌 **Available Commands**"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Start", callback_data="cmd_start"),
            InlineKeyboardButton("🗑 Delete", callback_data="cmd_delete")
        ],
        [
            InlineKeyboardButton("🆔 ID", callback_data="cmd_id"),
            InlineKeyboardButton("👑 Admin", callback_data="cmd_admin")
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="cmd_broadcast")
        ],
        [
            InlineKeyboardButton("🔙 Back To Home", callback_data="menu_home")
        ]
    ])

    await query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def menu_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "📢 **NOTE:**\n\n"
        "- ᴛʜɪꜱ ʙᴏᴛ ɪs ɴᴏᴛ ᴀɴ ᴏᴘᴇɴ sᴏᴜʀᴄᴇ ᴘʀᴏᴊᴇᴄᴛ."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back To Home", callback_data="menu_home")]
    ])

    await query.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def menu_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total_files = collection.count_documents({})
    total_users = "N/A"
    used_storage = "N/A"
    free_storage = "N/A"

    text = (
        f"★ 𝚃𝙾𝚃𝙰𝙻 𝙵𝙸𝙻𝙴𝚂: {total_files}\n"
        f"★ 𝚃𝙾𝚃𝙰𝙻 𝚄𝚂𝙴𝚁𝚂: {total_users}\n"
        f"★ 𝚄𝚂𝙴𝙳 𝚂𝚃𝙾𝚁𝙰𝙶𝙴: {used_storage}\n"
        f"★ 𝙵𝚁𝙴𝙴 𝚂𝚃𝙾𝚁𝙰𝙶𝙴: {free_storage}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back To Home", callback_data="menu_home")]
    ])

    await query.message.edit_text(text, reply_markup=keyboard)

async def menu_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()

async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ============================
# COMMAND HANDLERS
# ============================

async def id_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    response = (
        f"👤 Your ID: `{user_id}`\n"
        f"💬 Group ID: `{chat_id}`"
    )

    if update.message:
        await update.message.reply_text(response, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(response, parse_mode="Markdown")

async def admin_command(update: Update, context: CallbackContext):
    """Verify which admin IDs are loaded (accessible by anyone for testing)."""

    user_id = update.effective_user.id

    # ❌ FIX: handle both message & callback
    if not ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ No admin IDs configured!")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ No admin IDs configured!")
        return

    # Build admin list
    admin_list = []
    for i, admin_id in enumerate(sorted(ADMIN_IDS), 1):
        is_you = " 👈 **YOU**" if admin_id == user_id else ""
        admin_list.append(f"{i}. `{admin_id}`{is_you}")

    admin_text = "\n".join(admin_list)

    message = (
        f"👑 **Admin Verification**\n\n"
        f"📊 **Total Admins Loaded:** {len(ADMIN_IDS)}\n\n"
        f"🆔 **Admin IDs:**\n{admin_text}\n\n"
        f"👤 **Your ID:** `{user_id}`\n"
        f"✅ **Your Status:** {'**ADMIN**' if is_admin(user_id) else 'Regular User'}\n\n"
        f"💡 **Tip:** All users above should have admin privileges."
    )

    # ✅ FIX: reply correctly based on update type
    if update.message:
        await update.message.reply_text(message, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(message, parse_mode="Markdown")


# Temporary storage for broadcast sessions
broadcast_sessions = {}

async def broadcast_command(update: Update, context: CallbackContext):
    """
    Broadcast a message to the search group - Enable broadcast mode.
    
    ✅ Admin only
    ✅ Bot PM only
    """
    user_id = update.effective_user.id
    
    # 🚫 ADMIN CHECK
    if not is_admin(user_id):
        await update.message.reply_text("❌ Only admins can use this command.")
        return
    
    # 🔒 PRIVATE CHAT ONLY
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "❌ This command works only in private chat with the bot."
        )
        return
    
    # 📢 Enable broadcast mode
    broadcast_sessions[user_id] = {
        'active': True,
        'message': None,
        'photo': None,
        'caption': None
    }
    
    await update.message.reply_text(
        "📢 **Broadcast Mode Enabled**\n\n"
        "Send your message or image now.",
        parse_mode="Markdown"
    )

async def broadcast_message_handler(update: Update, context: CallbackContext):
    """Handle messages when broadcast mode is active."""
    
    user_id = update.effective_user.id
    
    # Check if user has active broadcast session
    if user_id not in broadcast_sessions or not broadcast_sessions[user_id].get('active'):
        return  # Not in broadcast mode
    
    session = broadcast_sessions[user_id]
    
    # 📸 Handle photo
    if update.message.photo:
        largest_photo = max(
            update.message.photo,
            key=lambda p: p.width * p.height
        )
        
        caption = update.message.caption or ""
        
        session['photo'] = largest_photo.file_id
        session['caption'] = sanitize_unicode(caption)
        session['message'] = None
        
        # Show confirmation buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel"),
                InlineKeyboardButton("✅ Send", callback_data="broadcast_send")
            ]
        ])
        
        # Show preview
        await update.message.reply_photo(
            photo=largest_photo.file_id,
            caption=f"📢 **Preview:**\n\n{caption if caption else '(No caption)'}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return
    
    # 📝 Handle text message
    if update.message.text:
        message_text = sanitize_unicode(update.message.text.strip())
        
        if not message_text:
            await update.message.reply_text("❌ Message cannot be empty.")
            return
        
        session['message'] = message_text
        session['photo'] = None
        session['caption'] = None
        
        # Show confirmation buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel"),
                InlineKeyboardButton("✅ Send", callback_data="broadcast_send")
            ]
        ])
        
        # Show preview
        await update.message.reply_text(
            f"📢 **Preview:**\n\n{message_text}",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

async def broadcast_callback_handler(update: Update, context: CallbackContext):
    """Handle broadcast confirmation buttons."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    # Check if user has active broadcast session
    if user_id not in broadcast_sessions:
        await query.message.edit_text("❌ No active broadcast session.")
        return
    
    session = broadcast_sessions[user_id]
    
    # ❌ CANCEL
    if data == "broadcast_cancel":
        del broadcast_sessions[user_id]
        await query.message.edit_text("❌ Broadcast cancelled.")
        return
    
    # ✅ SEND
    if data == "broadcast_send":
        try:
            # Send photo with caption
            if session.get('photo'):
                await context.bot.send_photo(
                    chat_id=SEARCH_GROUP_ID,
                    photo=session['photo'],
                    caption=session.get('caption', ''),
                    parse_mode="Markdown"
                )
                
                await query.message.edit_text(
                    "✅ **Broadcast sent successfully!**\n\n"
                    "📸 Image + Caption sent to search group.",
                    parse_mode="Markdown"
                )
            
            # Send text message
            elif session.get('message'):
                await context.bot.send_message(
                    chat_id=SEARCH_GROUP_ID,
                    text=session['message'],
                    parse_mode="Markdown"
                )
                
                await query.message.edit_text(
                    "✅ **Broadcast sent successfully!**\n\n"
                    "📤 Message sent to search group.",
                    parse_mode="Markdown"
                )
            
            else:
                await query.message.edit_text("❌ No message to send.")
            
            # Clean up session
            del broadcast_sessions[user_id]
            
        except Exception as e:
            logging.error(f"Broadcast error: {sanitize_unicode(str(e))}")
            await query.message.edit_text(
                f"❌ Failed to broadcast:\n`{sanitize_unicode(str(e))}`",
                parse_mode="Markdown"
            )
            
            # Keep session active on error
            session['active'] = True


async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to delete movies - ONLY in private chat."""

    user_id = update.effective_user.id

    # 🚫 ADMIN CHECK
    if not is_admin(user_id):
        if update.message:
            await update.message.reply_text("❌ Only admins can use this command.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Only admins can use this command.")
        return

    # 🔒 PRIVATE CHAT ONLY
    if update.message:
        if update.effective_chat.type != "private":
            await update.message.reply_text(
                "❌ This command works only in private chat with the bot."
            )
            return
    elif update.callback_query:
        if update.callback_query.message.chat.type != "private":
            await update.callback_query.answer(
                "❌ This command works only in private chat."
            )
            return
    else:
        return

    # 📄 Pagination
    page = int(context.args[0]) if context.args else 1
    skip = (page - 1) * PAGE_SIZE

    total = collection.count_documents({})
    movies = list(
        collection.find({})
        .sort("_id", -1)
        .skip(skip)
        .limit(PAGE_SIZE)
    )

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    if not movies:
        text = "No movies found."
    else:
        text = (
            f"🎬 **Total movies stored: {total}**\n"
            f"📄 **Page {page} / {total_pages}**\n\n"
        )
        for i, movie in enumerate(movies, start=skip + 1):
            text += f"{i}. {movie.get('name', 'Unknown Movie')}\n"

    # 💾 Save session
    delete_sessions[user_id] = {
        "page": page,
        "movies": movies
    }

    # ⬅️➡️ Buttons
    keyboard = []

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page:{page-1}"))
    if skip + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page+1}"))

    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("🗑 Delete", callback_data="ask_delete")
    ])

    keyboard.append([
        InlineKeyboardButton("🔙 Back To Home", callback_data="menu_home")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # 📤 Send / Edit
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

async def ask_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = delete_sessions.get(user_id)

    if not session:
        await query.message.reply_text("❌ No active list found.")
        return

    count = len(session["movies"])

    await query.message.reply_text(
        f"✏️ **Send the movie number to delete (1–{count})**",
        parse_mode="Markdown"
    )

async def delete_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ✅ Must be a normal text message
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    # ✅ Must have an active delete session - CHECK THIS FIRST!
    if user_id not in delete_sessions:
        return  # Not a delete session, let other handlers process it

    # ✅ Only NOW check if admin (since we have a delete session)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Only admins can delete movies.")
        return

    text = update.message.text.strip()

    # ✅ Only accept numbers
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid number.")
        return

    index = int(text) - 1
    session = delete_sessions[user_id]
    movies = session["movies"]
    page = session["page"]

    # ✅ Number range check
    if index < 0 or index >= len(movies):
        await update.message.reply_text(
            f"❌ Invalid number.\nPlease choose a number **from this page only**.",
            parse_mode="Markdown"
        )
        return

    movie = movies[index]

    # ✅ Store selected movie for confirmation
    delete_sessions[user_id]["selected"] = movie

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_del"),
            InlineKeyboardButton(
                "✅ Confirm",
                callback_data=f"confirm_del:{movie['_id']}:{page}"
            )
        ]
    ])

    await update.message.reply_text(
        f"⚠️ **Are you sure you want to delete:**\n\n🎬 **{movie.get('name', 'Unknown Movie')}**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])

    context.args = [str(page)]
    await list_movies(update, context)

async def confirm_number_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, movie_id, page = query.data.split(":")

    collection.delete_one({"_id": ObjectId(movie_id)})

    await query.message.edit_text("🗑 **Movie deleted successfully!**", parse_mode="Markdown")

    context.args = [page]
    await list_movies(update, context)

# ============================
# CALLBACK ROUTERS
# ============================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data.startswith("page:"):
        await paginate(update, context)

    elif data == "ask_delete":
        await ask_delete(update, context)

    elif data.startswith("confirm_del:"):
        await confirm_number_delete(update, context)

    elif data == "cancel_del":
        await update.callback_query.message.delete()

async def start_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "menu_home":
        await menu_home(update, context)

    elif data == "menu_comments":
        await menu_comments(update, context)

    elif data == "menu_source":
        await menu_source(update, context)

    elif data == "menu_status":
        await menu_status(update, context)

    elif data == "menu_close":
        await menu_close(update, context)

    elif data == "cmd_start":
        await start(update, context)

    elif data == "cmd_delete":
        await list_movies(update, context)

    elif data == "cmd_id":
        await id_command(update, context)

    elif data == "cmd_admin":
        await admin_command(update, context)
    
    elif data == "cmd_broadcast":
        # Check if admin
        user_id = query.from_user.id
        if not is_admin(user_id):
            await query.answer("❌ Only admins can use this command.", show_alert=True)
            return
        
        # Check if in private chat
        if query.message.chat.type != "private":
            await query.answer("❌ This command only works in private chat.", show_alert=True)
            return
        
        # 📢 Enable broadcast mode directly
        broadcast_sessions[user_id] = {
            'active': True,
            'message': None,
            'photo': None,
            'caption': None
        }
        
        await query.message.reply_text(
            "📢 **Broadcast Mode Enabled**\n\n"
            "Send your message or image now.",
            parse_mode="Markdown"
        )
        await query.answer("✅ Broadcast mode enabled")

        
# ============================
# WEB SERVER & KEEP AWAKE
# ============================

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
    max_retries = 5
    retry_delay = 10

    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        logging.info("✅ Ping successful: Bot is awake")
                        return
                    else:
                        logging.warning(f"⚠️ Ping failed (status {resp.status}), retrying...")
            except Exception as e:
                logging.error(f"❌ Error pinging self: {e}")

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)

    logging.critical("🚨 Max retries reached. Bot might be inactive!")

aiocron.crontab("*/5 * * * *", func=keep_awake)

# ============================
# MAIN FUNCTION
# ============================

async def main():
    """Main function to start the bot."""
    try:
        await start_web_server()

        application = ApplicationBuilder().token(TOKEN).build()
        
        # HANDLER ORDER MATTERS! Add specific handlers first

        # 1. Command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("id", id_command))
        application.add_handler(CommandHandler("delete", list_movies))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CommandHandler("broadcast", broadcast_command))

        # 2. Callback handlers
        application.add_handler(CallbackQueryHandler(start_menu_router, pattern="^(menu_|cmd_)"))
        application.add_handler(CallbackQueryHandler(language_callback_handler, pattern="^lang_"))
        application.add_handler(CallbackQueryHandler(name_decision_handler, pattern="^(edit_name|continue_name)$"))
        application.add_handler(CallbackQueryHandler(broadcast_callback_handler, pattern="^broadcast_"))
        application.add_handler(CallbackQueryHandler(callback_router,pattern="^(page:|ask_delete|confirm_del:|cancel_del)"))

        # 3. Search handler - MUST COME BEFORE delete_by_number
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(SEARCH_GROUP_ID),
            search_movie
        ))

        # 4. File/Photo upload handlers
        application.add_handler(MessageHandler(
            filters.Document.ALL & filters.Chat(STORAGE_GROUP_ID), 
            add_movie
        ))
        application.add_handler(MessageHandler(
            filters.PHOTO & filters.Chat(STORAGE_GROUP_ID), 
            add_movie
        ))
        
        # 5. Text handler for name editing
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(STORAGE_GROUP_ID),
            text_handler
        ))
        
        # 6. Delete handler
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            delete_by_number
        ))

        # 7. Broadcast message handler 
        application.add_handler(MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.ChatType.PRIVATE,
            broadcast_message_handler
        ))


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
