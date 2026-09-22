from fastapi import APIRouter

from app.api.routes import admin, chat, conversations, documents, feedback, memories, voice

api_router = APIRouter()
api_router.include_router(chat.router)
api_router.include_router(voice.router)
api_router.include_router(conversations.router)
api_router.include_router(memories.router)
api_router.include_router(documents.router)
api_router.include_router(feedback.router)
api_router.include_router(admin.router)
