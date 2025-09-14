from flask_sqlalchemy import SQLAlchemy
from flask import Flask

app = Flask(__name__)
# To be fixed, it has to load the correct DB path from tenant.json
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///db/bgs_data.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)

with app.app_context():
    db.create_all()
    print("✅ Tabelle 'users' wurde erstellt.")
