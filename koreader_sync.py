import os
import time
import sqlite3
from contextlib import contextmanager
import argparse
import logging
import traceback
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify

USERSDB = os.environ.get("KOSYNC_DATABASE", "sync.sqlite3")

# TODO return content type application/vnd.koreader.v1+json

# SQLite storage. Each operation gets its own connection and transaction.
# Standard rollback journaling keeps the database local and durable.
@contextmanager
def database():
    connection = sqlite3.connect(USERSDB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def initDb():
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY NOT NULL,
                userkey TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS progress (
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                document TEXT NOT NULL,
                progress TEXT,
                percentage REAL,
                device TEXT,
                device_id TEXT,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (username, document)
            );
        """)

def getUser(userName):
    with database() as db:
        row = db.execute(
            "SELECT username, userkey FROM users WHERE username = ?", (userName,)
        ).fetchone()
        return dict(row) if row is not None else None

def addUser(userName, userKey):
    with database() as db:
        result = db.execute(
            "INSERT INTO users (username, userkey) VALUES (?, ?) "
            "ON CONFLICT(username) DO NOTHING", (userName, userKey)
        )
        return result.rowcount == 1

def getPosition(username, document):
    with database() as db:
        row = db.execute(
            "SELECT document, progress, percentage, device, device_id, timestamp "
            "FROM progress WHERE username = ? AND document = ?",
            (username, document)
        ).fetchone()
        return dict(row) if row is not None else {}

def updatePosition(username, document, position):
    timestamp = int(time.time())
    with database() as db:
        db.execute("""
            INSERT INTO progress
                (username, document, progress, percentage, device, device_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, document) DO UPDATE SET
                progress = excluded.progress,
                percentage = excluded.percentage,
                device = excluded.device,
                device_id = excluded.device_id,
                timestamp = excluded.timestamp
        """, (username, document, position.get('progress'), position.get('percentage'),
              position.get('device'), position.get('device_id'), timestamp))
    return timestamp

### Database stuff ends here

# Web Server stuff

app = Flask(__name__)

@app.get("/healthcheck")
def healthcheck():
    return {"state": "OK"}

class ServiceError(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        super(ServiceError, self).__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        thePayload = self.payload
        if (thePayload):
            # Convert non-iterable payload to an iterable
            try:
                iter(thePayload)
            except:
                thePayload = (thePayload)
        rv = dict(thePayload or ())
        rv['message'] = str(self)
        return rv

def logException(exception):
    app.logger.error("".join(traceback.format_exception(type(exception), exception, exception.__traceback__)))

@app.errorhandler(ServiceError)
def handle_service_error(error):
    logException(error)
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response

def authorizeRequest(request):
    username = request.headers.get("x-auth-user")
    userkey = request.headers.get("x-auth-key")
    if (username == None or userkey == None):
        raise ServiceError('Unauthorized', status_code=401)

    user = getUser(username)
    if (user == None):
        raise ServiceError('Forbidden', status_code=403)
    if (userkey != user['userkey']):
        raise ServiceError('Unauthorized', status_code=401)
    return user

# API

@app.route('/users/create', methods = ['POST'])
def register():
    try:
        if (request.is_json):
            user = request.get_json()
            username = user.get('username')
            userkey = user.get('password')
            if (username == None or userkey == None):
                return 'Invalid request', 400
            if (not addUser(username, userkey)):
                return 'Username is already registered.', 409
            return jsonify(dict(username=username)), 201
        else:
            return 'Invalid request', 400
    except Exception as e:
        raise ServiceError('Unknown server error', status_code=500) from e

@app.route('/users/auth')
def authorize():
    try:
        authorizeRequest(request)
        return jsonify(dict(authorized='OK')), 200
    except ServiceError as se:
        raise
    except Exception as e:
        raise ServiceError('Unknown server error', status_code=500) from e

@app.route('/syncs/progress/<document>')
def getProgress(document):
    try:
        user = authorizeRequest(request)
        position = getPosition(user['username'], document)
        return jsonify(position), 200
    except ServiceError as se:
        raise
    except Exception as e:
        raise ServiceError('Unknown server error', status_code=500) from e

@app.route('/syncs/progress', methods = ['PUT'])
def updateProgress():
    try:
        user = authorizeRequest(request)
        if (request.is_json):
            position = request.get_json()
            document = position.get('document')
            timestamp = updatePosition(user['username'], document, position)
            return jsonify(dict(document = document, timestamp = timestamp)), 200
        else:
            return 'Invalid request', 400
    except ServiceError as se:
        raise
    except Exception as e:
        raise ServiceError('Unknown server error', status_code=500) from e

# Initialization

def main():
    global USERSDB
    parser = argparse.ArgumentParser(description="KOReader Sync Server")
    parser.add_argument("-d", "--database", type = str, default=USERSDB, help = "SQLite database file")
    parser.add_argument("-t", "--host", type = str, default="0.0.0.0", help = "Server host")
    parser.add_argument("-p", "--port", type = int, default=8082, help = "Server port")
    parser.add_argument("-c", "--certificate", type = str, help = "SSL Certificate file")
    parser.add_argument("-k", "--key", type = str, help = "SSL Private key file")
    parser.add_argument("-l", "--logfile", type = str, default='koreader-server.log', help = "Log file")
    parser.add_argument("-v", "--verbose", action='store_true', help = "Run server in debug mode")
    args = parser.parse_args()

    USERSDB = args.database
    initDb()
    handler = RotatingFileHandler(args.logfile, maxBytes=100000, backupCount=1)
    handler.setLevel(logging.DEBUG)
    logging.getLogger('werkzeug').addHandler(handler) # HTTP log goes here
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)

    context = None
    if (args.certificate and args.key):
        context = (args.certificate, args.key)
    app.run(debug=args.verbose,host=args.host, port=args.port, ssl_context=context)

if __name__ == "__main__":
    main()
