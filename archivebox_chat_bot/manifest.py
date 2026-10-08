"""Downloadable Slack manifests for self-hosted and distributable installs."""


def manifest(role="capture", transport="socket", public_url="", connection_id="default"):
    ai = role == "ai"
    name = "ArchiveBox AI Bot" if ai else "ArchiveBox Bot"
    result = {
        "display_information": {
            "name": name,
            "description": "Your team's web archive, right in Slack",
            "background_color": "#16342f",
        },
        "features": {
            "bot_user": {"display_name": "archiveboxai" if ai else "archivebox", "always_online": True},
            "app_home": {
                "home_tab_enabled": True,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "channels:history",
                    "channels:read",
                    "groups:history",
                    "groups:read",
                    "im:history",
                    "chat:write",
                    "reactions:write",
                    "users:read",
                ]
            }
        },
        "settings": {
            "socket_mode_enabled": transport == "socket",
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
            "event_subscriptions": {
                "bot_events": ["app_mention", "message.channels", "message.groups", "message.im", "app_home_opened"]
            },
        },
    }
    if ai:
        result["features"]["agent_view"] = {
            "agent_description": "Your ArchiveBox agent. Search saved pages and manage your web archive using your configured OpenCode instance.",
            "suggested_prompts": [
                {"title": "Explore my archive", "message": "What is in my archive?"},
                {"title": "Find saved pages", "message": "Help me find pages in my archive."},
                {"title": "Save a link", "message": "Help me archive a URL."},
            ],
        }
    if ai:
        result["settings"]["event_subscriptions"]["bot_events"].append("agent_session_stopped")
        if "assistant:write" not in result["oauth_config"]["scopes"]["bot"]:
            result["oauth_config"]["scopes"]["bot"].append("assistant:write")
    if not ai:
        result["oauth_config"]["scopes"]["bot"] += [
            "channels:manage",
            "channels:join",
            "commands",
            "files:write",
            "files:read",
        ]
        command = {
            "command": "/archivebox",
            "description": "Save, search, or check your archive",
            "usage_hint": "save <URLs> | search <query> | status | help",
            "should_escape": True,
        }
        if transport == "http":
            command["url"] = f"{public_url}/connections/{connection_id}/slack/{role}/commands"
        result["features"]["slash_commands"] = [command]
    if transport == "http":
        result["settings"]["event_subscriptions"]["request_url"] = (
            f"{public_url}/connections/{connection_id}/slack/{role}/events"
        )
        result["oauth_config"]["redirect_urls"] = [
            f"{public_url}/connections/{connection_id}/slack/{role}/oauth/callback"
        ]
    return result
