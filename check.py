import discord

print("FILE:", discord.__file__)
print("VERSION_INFO:", getattr(discord, "version_info", "MISSING"))
print("HAS INTENTS:", hasattr(discord, "Intents"))
print("HAS REFERENCED_MESSAGE:", hasattr(discord.Message, "referenced_message"))
print("HAS TO_DICT:", hasattr(discord.Message, "to_dict"))