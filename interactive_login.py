import sys
import asyncio
import os
from telethon import TelegramClient

api_id = "30449447"
api_hash = "ec0f8e959edb27bc595b05f6b465bf04"
phone_number = sys.argv[1]
session_file = os.path.expanduser("~/.tele_backup_session")

async def main():
    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()
    
    if not await client.is_user_authorized():
        print(f"Requesting code for {phone_number}...")
        try:
            await client.send_code_request(phone_number)
            print("CODE_REQUESTED")
            code = input("Enter code: ").strip()
            print("Logging in with code...")
            await client.sign_in(phone_number, code)
            print("LOGIN_SUCCESS")
        except Exception as e:
            print(f"LOGIN_ERROR: {e}")
    else:
        print("ALREADY_LOGGED_IN")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
