import os
from datetime import datetime, timedelta

from flask import Flask, request, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# Optional: load .env locally (Render/Heroku will inject env vars)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

# --- Database setup ---
db_url = os.getenv("DATABASE_URL")
if not db_url:
    # local fallback
    db_url = "sqlite:///subscriptions.db"
# Render/Heroku sometimes provide postgres://; SQLAlchemy needs postgresql://
db_url = db_url.replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --- Auth setup ---
login_manager = LoginManager(app)
login_manager.login_view = "login"


# --- Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # (Legacy) kept for backward compatibility; ignored in UI/logic now
    premium = db.Column(db.Boolean, default=True)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    renewal_date = db.Column(db.Date, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    user = db.relationship(User, backref=db.backref("subscriptions", lazy=True))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --- Routes ---
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    msg = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        price_raw = (request.form.get("price") or "").strip()
        date_str = (request.form.get("date") or "").strip()

        if not name or not price_raw:
            flash("Please fill in name and price.", "warning")
            return redirect(url_for("index"))

        try:
            price = float(price_raw)
        except ValueError:
            flash("Price must be a number.", "warning")
            return redirect(url_for("index"))

        renewal_date = None
        if date_str:
            try:
                renewal_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                flash("Date must be in YYYY-MM-DD format.", "warning")
                return redirect(url_for("index"))

        sub = Subscription(name=name, price=price, renewal_date=renewal_date, user_id=current_user.id)
        db.session.add(sub)
        db.session.commit()
        flash(f"Added subscription: {name}", "success")
        return redirect(url_for("index"))

    subs = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.name.asc()).all()
    total = sum(s.price for s in subs)
    # “Due soon” flag = within next 7 days
    today = datetime.utcnow().date()
    due_soon_ids = {
        s.id for s in subs
        if s.renewal_date and 0 <= (s.renewal_date - today).days <= 7
    }
    return render_template("index.html", subscriptions=subs, total=total, due_soon_ids=due_soon_ids)


@app.route("/delete/<int:sub_id>", methods=["POST", "GET"])
@login_required
def delete(sub_id):
    sub = Subscription.query.filter_by(id=sub_id, user_id=current_user.id).first_or_404()
    db.session.delete(sub)
    db.session.commit()
    flash("Subscription deleted.", "info")
    return redirect(url_for("index"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()

        if not email or not password:
            flash("Email and password are required.", "warning")
            return redirect(url_for("signup"))

        if User.query.filter_by(email=email).first():
            flash("Email already registered — try logging in.", "warning")
            return redirect(url_for("login"))

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created — you're now logged in.", "success")
        return redirect(url_for("index"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Logged in", "info")
            return redirect(url_for("index"))
        else:
            flash("Invalid email or password", "warning")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
