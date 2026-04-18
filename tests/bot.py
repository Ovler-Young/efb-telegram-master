import os


def get_env(key):
    val = os.getenv(key.upper())
    if val:
        return val
    raise ValueError(f"{key} is not defined")


def get_bot():
    aux_bot_tokens = []
    for env_key in ("aux_bot_token", "aux_bot_token_2"):
        val = os.getenv(env_key.upper())
        if val:
            aux_bot_tokens.append(val)

    topic_group = os.getenv("TOPIC_GROUP")
    return {
        'token': get_env('token'),
        'admins': list(map(int, get_env('admins').split(','))),
        'groups': list(map(int, get_env('groups').split(','))),
        'channels': list(map(int, get_env('channels').split(','))),
        'topic_group': int(topic_group) if topic_group else None,
        'aux_bot_tokens': aux_bot_tokens,
        'aux_bot_ids': [int(token.split(":", 1)[0]) for token in aux_bot_tokens],
    }


def get_user_session():
    return {
        'user_session': get_env('user_session'),
        'api_id': int(get_env('api_id')),
        'api_hash': get_env('api_hash')
    }
