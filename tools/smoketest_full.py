#!/usr/bin/env python3
# Offline smoketest for NixeBot phish pipeline cogs (no Discord login required)
import os, sys, json, types, ast, importlib

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def install_discord_stub():
    discord = types.ModuleType("discord")
    class _Exc(Exception): pass
    class Forbidden(_Exc): pass
    class HTTPException(_Exc): pass
    class NotFound(_Exc): pass
    discord.Forbidden=Forbidden
    discord.HTTPException=HTTPException
    discord.NotFound=NotFound

    class Attachment:
        def __init__(self, filename="x.png", url="http://example/x.png", size=10, content_type="image/png"):
            self.filename=filename; self.url=url; self.size=size; self.content_type=content_type
        async def read(self): return b""
    discord.Attachment=Attachment

    class _Asset:
        def __init__(self, url=""): self.url=url

    class Embed:
        def __init__(self, image_url=""):
            self.image=_Asset(image_url) if image_url else None
            self.thumbnail=_Asset("")
    discord.Embed=Embed

    class User:
        def __init__(self, id=1, bot=False): self.id=id; self.bot=bot

    class Channel:
        def __init__(self, id=1, name="chan"): self.id=id; self.name=name

    class Guild:
        def __init__(self, id=1, name="guild"): self.id=id; self.name=name

    class Message:
        def __init__(self, content="", author=None, channel=None, attachments=None, embeds=None, guild=None, id=1):
            self.content=content
            self.author=author or User()
            self.channel=channel or Channel()
            self.guild=guild or Guild()
            self.attachments=attachments or []
            self.embeds=embeds or []
            self.id=id
        async def delete(self, **kwargs): return
    discord.Message=Message

    ext = types.ModuleType("discord.ext")
    commands = types.ModuleType("discord.ext.commands")
    class Cog:
        @classmethod
        def listener(cls, name=None):
            def deco(fn):
                fn.__listener_name__=name
                return fn
            return deco
    commands.Cog=Cog
    def _deco(*args, **kwargs):
        def deco(fn): return fn
        return deco
    commands.command=_deco
    commands.hybrid_command=_deco
    commands.has_permissions=_deco
    commands.cooldown=_deco
    commands.BucketType=types.SimpleNamespace(user=1, guild=2)
    class Bot: pass
    commands.Bot=Bot

    ext.commands=commands
    discord.ext=ext

    sys.modules["discord"]=discord
    sys.modules["discord.ext"]=ext
    sys.modules["discord.ext.commands"]=commands

install_discord_stub()

def assert_listener(module_path: str, class_name: str, fn_name: str, event_name: str):
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    # Runtime attribute check
    fn = getattr(cls, fn_name, None)
    assert fn is not None, f"{module_path}.{class_name} missing {fn_name}"
    # Source decorator check (AST)
    src_path = os.path.join(PROJECT_ROOT, *module_path.split(".")) + ".py"
    src = open(src_path, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == fn_name:
                    for d in m.decorator_list:
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "listener":
                            # event arg can be absent or explicit
                            if d.args and isinstance(d.args[0], ast.Constant) and d.args[0].value == event_name:
                                return
                            if not d.args and event_name in ("on_message",):
                                return
    raise AssertionError(f"{module_path}.{class_name}.{fn_name} missing @listener({event_name}) decorator")

def main():
    # runtime env basic parse
    env_path = os.path.join(PROJECT_ROOT, "nixe", "config", "runtime_env.json")
    with open(env_path, "r", encoding="utf-8") as f:
        env = json.load(f)
    # NOTE: PHISH_REQUIRE_OCR_FOR_BAN is OPTIONAL in code (defaults to "1" via os.getenv).
    required = ["PHISH_AUTOBAN", "PHISH_AUTOBAN_ON_PHASH", "PHISH_PHASH_EXTS"]
    optional = {"PHISH_REQUIRE_OCR_FOR_BAN": "1"}

    missing = [k for k in required if k not in env]
    assert not missing, f"runtime_env missing required keys: {missing}"

    for k, default in optional.items():
        if k not in env:
            print(f"[WARN] runtime_env missing optional key {k}; code default={default!r} will apply")

    # Import cogs
    importlib.import_module("nixe.cogs.a16_phash_phish_guard_overlay")
    importlib.import_module("nixe.cogs.a00_everyone_spam_autoban_overlay")
    importlib.import_module("nixe.cogs.phish_ban_embed")
    importlib.import_module("nixe.cogs.a16_sus_attach_hardener_overlay")

    # Listener checks
    assert_listener("nixe.cogs.a16_sus_attach_hardener_overlay","SusAttachHardener","_on_message","on_message")
    assert_listener("nixe.cogs.phish_ban_embed","PhishBanEmbed","on_nixe_phish_detected","on_nixe_phish_detected")

    print("SMOKETEST PASS")

if __name__ == "__main__":
    main()
