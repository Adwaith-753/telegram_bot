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
from telegram.ext import ContextTypes
from bson import ObjectId


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
PAGE_SIZE = 10
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

delete_sessions = {}

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




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_name = context.bot.first_name
    args = context.args

    # 🔹 Deep link movie handling
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
            InlineKeyboardButton("💬 Comments", callback_data="menu_comments"),
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



async def menu_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📌 **Available Commands**"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Start Bot", callback_data="cmd_start"),
            InlineKeyboardButton("🔍 Search Movies", callback_data="cmd_search")
        ],
        [
            InlineKeyboardButton("📂 Movie List", callback_data="cmd_list"),
            InlineKeyboardButton("🆔 Get IDs", callback_data="cmd_id")
        ],
        [
            InlineKeyboardButton("🔙 Back To Home", callback_data="menu_home")
        ]
    ])

    await query.message.edit_text(text,reply_markup=keyboard,parse_mode="Markdown")





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

# Define the /list command (paginated list)
async def list_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    page = int(context.args[0]) if context.args else 1
    skip = (page - 1) * PAGE_SIZE

    total = collection.count_documents({})
    movies = list(
        collection.find({})
        .sort("name", 1)
        .skip(skip)
        .limit(PAGE_SIZE)
    )

    if not movies:
        text = "No movies found."
    else:
        text = f"🎬 **Total movies stored: {total}**\n\n"
        for i, movie in enumerate(movies, start=skip + 1):
            text += f"{i}. {movie.get('name', 'Unknown Movie')}\n"

    # Save movies for delete-by-number
    delete_sessions[update.effective_user.id] = {
        "page": page,
        "movies": movies
    }

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

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.callback_query.message.edit_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
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
    # ✅ Only admins can delete
    if update.effective_user.id not in ADMIN_IDS:
        return

    # ✅ Must be a normal text message
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    # ✅ Must have an active delete session
    if user_id not in delete_sessions:
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
        await update.message.reply_text(f"❌ Invalid number.\nPlease choose a number **from this page only**.",parse_mode="Markdown")
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


# Pagination handler
async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])

    context.args = [str(page)]
    await list_movies(update, context)


#Delete confirmation dialog
async def confirm_number_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, movie_id, page = query.data.split(":")

    collection.delete_one({"_id": ObjectId(movie_id)})

    await query.message.edit_text("🗑 **Movie deleted successfully!**", parse_mode="Markdown")

    context.args = [page]
    await list_movies(update, context)


# Callback router
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

    # ===== MAIN MENU =====
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

    # ===== COMMAND BUTTONS =====
    elif data == "cmd_start":
        # Restart home menu cleanly
        await menu_home(update, context)

    elif data == "cmd_search":
        await query.answer(
            "🔍 Type the movie name in the SEARCH GROUP",
            show_alert=True
        )

    elif data == "cmd_list":
        if query.from_user.id in ADMIN_IDS:
            # Call list directly (no text spam)
            context.args = []
            await list_movies(update, context)
        else:
            await query.answer("❌ Admin only command", show_alert=True)

    elif data == "cmd_id":
        user_id = query.from_user.id
        chat_id = query.message.chat.id

        await query.message.reply_text(
            f"🆔 **Your ID Info**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💬 Chat ID: `{chat_id}`",
            parse_mode="Markdown"
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
        
        # Add import for filters
        from telegram.ext import filters
        
        # HANDLER ORDER MATTERS! Add specific handlers first

        # 1. Command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("id", id_command))
        application.add_handler(CommandHandler("list", list_movies))

        #MENU BUTTONS (/start menu)
        application.add_handler(CallbackQueryHandler(start_menu_router, pattern="^menu_"))

        #MOVIE UPLOAD NAME EDIT
        application.add_handler(CallbackQueryHandler(name_decision_handler, pattern="^(edit_name|continue_name)$"))

        #MOVIE DOWNLOAD BUTTON
        application.add_handler(CallbackQueryHandler(get_movie_files, pattern="^movie_"))

        #LIST / DELETE / PAGINATION
        application.add_handler(CallbackQueryHandler(callback_router,pattern="^(page:|ask_delete|confirm_del:|cancel_del)"))


        # 3. File/Photo upload handlers - ONLY in storage group
        application.add_handler(MessageHandler(
            filters.Document.ALL & filters.Chat(STORAGE_GROUP_ID), 
            add_movie
        ))
        application.add_handler(MessageHandler(
            filters.PHOTO & filters.Chat(STORAGE_GROUP_ID), 
            add_movie
        ))
        
        # 4. Text handler - ONLY in storage group (for name editing)
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(STORAGE_GROUP_ID),
            text_handler
        ))
        # 4. Text handler -(delete_by_number)
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            delete_by_number
        ))

        # 5. Search handler - ONLY in search group
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Chat(SEARCH_GROUP_ID),
            search_movie
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
