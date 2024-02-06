import slack
import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask
from slackeventsapi import SlackEventAdapter
from pymongo.mongo_client import MongoClient
import re

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

app = Flask(__name__)

slack_event_adapter = SlackEventAdapter(os.environ['SIGNING_SECRET'],'/slack/events',app)
client = slack.WebClient(token=os.environ['SLACK_TOKEN'])
bot_id = client.auth_test()['user_id']

mongo = MongoClient(os.environ['DATABASE_URL'])
db = mongo.praise
users = db["users"]

def record_praise(timestamp, praised, praiser, reason):
    user = users.find_one({'_id': praised})

    praise = {
        'timestamp': timestamp,
        'praiser': praiser,
        'reason': reason,
        'upvotes': 1
    }
    
    if user is None:
        users.insert_one({'_id': praised, 'praises': [praise]})
    else:
        users.update_one({'_id': praised}, {'$push': {'praises': praise}}, upsert=True)
    
    updated_user = users.find_one({'_id': praised})
    
    return sum(p['upvotes'] for p in updated_user.get('praises', []))

def record_post(praised, praise_timestamp, post_timestamp):
    user = users.find_one({'_id': praised})
    
    if user is not None:
        praises = user.get('praises', [])

        praise_entry = next((p for p in praises if p.get('timestamp') == praise_timestamp), None)

        if praise_entry is not None:
            praise_entry['post_timestamp'] = post_timestamp
            users.update_one({'_id': praised}, {'$set': {'praises': praises}})
    
    return

def record_vote(timestamp, praiser, type):
    all_users = users.find({})

    for user in all_users:
        praises = user.get('praises', [])
        praise_entry = next((p for p in praises if p.get('timestamp') == timestamp), None)

        if praise_entry is not None:
            if praise_entry['praiser'] == praiser or user["_id"] == praiser:
                return None, None, None, None, None, None

            if type == 'reaction':
                praise_entry['upvotes'] += 1
            elif type == 'unreaction':
                praise_entry['upvotes'] -= 1

            users.update_one({'_id': user['_id']}, {'$set': {'praises': praises}})

            praised = user['_id']
            reason = praise_entry.get('reason')
            praiser = praise_entry.get('praiser')
            reason_upvotes = praise_entry['upvotes']
            total_upvotes = sum(p['upvotes'] for p in praises)
            post_timestamp = praise_entry.get('post_timestamp')

            return praised, praiser, reason, reason_upvotes, total_upvotes, post_timestamp
        
def get_top_users():
    top_users_pipeline = [
        {
            "$unwind": "$praises"
        },
        {
            "$group": {
                "_id": "$_id",
                "total_upvotes": {
                    "$sum": "$praises.upvotes"
                }
            }
        },
        {
            "$sort": {"total_upvotes": -1}
        },
        {
            "$limit": 10
        }
    ]

    top_users = users.aggregate(top_users_pipeline)
 
    return top_users

def get_user_praises(user_id):
    user = users.find_one({'_id': user_id})

    if user is None:
        return None

    praises = user.get('praises', [])

    sorted_praises = sorted(praises, key=lambda x: x.get('upvotes', 0), reverse=True)

    message = f"*<@{user_id}> praises:*\n"
    for idx, praise in enumerate(sorted_praises, start=1):
        upvotes = praise.get('upvotes', 0)
        reason = praise.get('reason', 'No reason provided')
        message += f"{idx}.{reason.capitalize()} ({upvotes} upvotes)\n"

    return message

@slack_event_adapter.on('message')
def message(payload):
    event = payload.get('event', {})
    channel_id = event.get('channel')
    user_id = event.get('user')
    text = event.get('text')
    ts = event.get('ts')

    if not text:
        return
    
    if user_id == bot_id:
        return
    
    if text.lower().startswith(";praises"):
        if text.lower() == ";praises":
            result = get_user_praises(user_id)
        else:
            match = re.match(r";praises <@(\w+)>", text)
            if match:
                user_id = match.group(1)
                result = get_user_praises(user_id)

        if result is None:
            client.chat_postMessage(channel=channel_id, text=f"<@{user_id}> doesn't have any praises yet.")
            return
        else:
            client.chat_postMessage(channel=channel_id, text=f"{result}")
            return
    
    if text.lower() == ";top":
        top_users = get_top_users()

        response_message = "*Top 10 Users:*\n"
        rank = 1
        for user in top_users:
            response_message += f"{rank}. <@{user['_id']}> - {user['total_upvotes']} upvotes\n"
            rank += 1
        client.chat_postEphemeral(channel=channel_id, text=response_message, user=user_id)
        return

    if text.startswith(f"<@") and "> ++ for" in text: 
        mentioned_user_id = text.split("<@")[1].split(">")[0]
        reason = text.split("for", 1)[1].strip()

        if mentioned_user_id == user_id:
            client.chat_postEphemeral(channel=channel_id, text=f"<@{mentioned_user_id}>, you cannot praise yourself!", user=user_id)
            return

        upvotes = record_praise(ts, mentioned_user_id, user_id, reason)

        client.reactions_add(channel=channel_id, name='heavy_plus_sign', timestamp=ts)

        post = client.chat_postMessage(channel=channel_id, text=f"<@{mentioned_user_id}> received a praise from <@{user_id}> for {reason}\n\nThey now have *1 upvote* for this and *{upvotes} upvotes* total 👏", thread_ts=ts)

        record_post(mentioned_user_id, ts, post['ts'])
    return
    
@slack_event_adapter.on('reaction_added')
def reaction_added(payload):
    event = payload.get('event', {})
    user_id = event.get('user')
    reaction = event.get('reaction')
    item = event.get('item', {})
    ts = item.get('ts')
    channel_id = item.get('channel')

    if user_id == bot_id:
        return
    
    if reaction != "heavy_plus_sign":
        return
    
    praised, praiser, reason, reason_upvotes, total_upvotes, post_timestamp = record_vote(ts, user_id, 'reaction')

    if praised is None or praiser is None or reason is None or reason_upvotes is None or total_upvotes is None or post_timestamp is None:
        client.chat_postEphemeral(channel=channel_id, text=f"<@{user_id}>, you cannot praise yourself or someone you've already praised for this reason!", user=user_id)
        return
    
    if reason_upvotes > 1:
        reason_upvotes = f'*{reason_upvotes} upvotes*'
    else:
        reason_upvotes = f'*{reason_upvotes} upvote*'

    post = client.chat_update(channel=channel_id, ts=post_timestamp, text=f"<@{praised}> received a praise from <@{praiser}> for {reason}\n\nThey now have {reason_upvotes} for this and *{total_upvotes} upvotes* total 👏")
    record_post(praised, ts, post['ts'])

@slack_event_adapter.on('reaction_removed')
def reaction_added(payload):
    event = payload.get('event', {})
    reaction = event.get('reaction')
    user_id = event.get('user')
    item = event.get('item', {})
    ts = item.get('ts')
    channel_id = item.get('channel')

    if user_id == bot_id:
        return
    
    if reaction != "heavy_plus_sign":
        return

    praised, praiser, reason, reason_upvotes, total_upvotes, post_timestamp = record_vote(ts, user_id, 'unreaction')

    if praised is None or praiser is None or reason is None or reason_upvotes is None or total_upvotes is None or post_timestamp is None:
        client.chat_postEphemeral(channel=channel_id, text=f"<@{user_id}>, you cannot unpraise yourself or someone you've already praised for this reason!", user=user_id)
        return
    
    if reason_upvotes > 1:
        reason_upvotes = f'*{reason_upvotes} upvotes*'
    else:
        reason_upvotes = f'*{reason_upvotes} upvote*'

    post = client.chat_update(channel=channel_id, ts=post_timestamp, text=f"<@{praised}> received a praise from <@{praiser}> for {reason}\n\nThey now have {reason_upvotes} for this and *{total_upvotes} upvotes* total 👏")
    record_post(praised, ts, post['ts'])

if __name__ == "__main__":
    app.run(port=80)
