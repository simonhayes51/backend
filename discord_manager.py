# discord_manager.py
import os
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

class DiscordManager:
    def __init__(self):
        self.bot_token = os.getenv("DISCORD_BOT_TOKEN")
        self.guild_id = os.getenv("DISCORD_SERVER_ID")
        self.premium_role_id = os.getenv("DISCORD_PREMIUM_ROLE_ID")
        
    async def assign_premium_role(self, discord_user_id: str) -> bool:
        """Assign premium role to Discord user"""
        if not all([self.bot_token, self.guild_id, self.premium_role_id]):
            logger.warning("Discord bot configuration incomplete")
            return False
            
        url = f"https://discord.com/api/v10/guilds/{self.guild_id}/members/{discord_user_id}/roles/{self.premium_role_id}"
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, headers=headers) as resp:
                    if resp.status in (200, 204):
                        logger.info(f"✅ Assigned premium role to {discord_user_id}")
                        return True
                    else:
                        logger.error(f"❌ Failed to assign role: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ Error assigning Discord role: {e}")
            return False
    
    async def remove_premium_role(self, discord_user_id: str) -> bool:
        """Remove premium role from Discord user"""
        if not all([self.bot_token, self.guild_id, self.premium_role_id]):
            return False
            
        url = f"https://discord.com/api/v10/guilds/{self.guild_id}/members/{discord_user_id}/roles/{self.premium_role_id}"
        headers = {"Authorization": f"Bot {self.bot_token}"}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=headers) as resp:
                    if resp.status in (200, 204):
                        logger.info(f"✅ Removed premium role from {discord_user_id}")
                        return True
                    else:
                        logger.error(f"❌ Failed to remove role: {resp.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ Error removing Discord role: {e}")
            return False

# Global instance
discord_manager = DiscordManager()
