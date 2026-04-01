import os
import re
from contextlib import closing

import mysql.connector
import qrcode
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from mysql.connector import Error

load_dotenv()


def get_db_config():
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "vending_machine"),
    }


def get_connection():
    return mysql.connector.connect(**get_db_config())


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def get_items():
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute("SELECT id, name, price, stock FROM items ORDER BY id")
        return cursor.fetchall()


def get_item(item_id):
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute("SELECT id, name, price, stock FROM items WHERE id = %s", (item_id,))
        return cursor.fetchone()


def create_order(total):
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO orders (total, status) VALUES (%s, %s)",
            (total, "PENDING"),
        )
        conn.commit()
        return cursor.lastrowid


def add_order_item(order_id, item_id, qty):
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO order_items (order_id, item_id, quantity) VALUES (%s, %s, %s)",
            (order_id, item_id, qty),
        )
        conn.commit()


def update_stock(item_id, qty):
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute(
            "UPDATE items SET stock = stock - %s WHERE id = %s AND stock >= %s",
            (qty, item_id, qty),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_paid(order_id):
    with closing(get_connection()) as conn, closing(conn.cursor()) as cursor:
        cursor.execute("UPDATE orders SET status = 'PAID' WHERE id = %s", (order_id,))
        conn.commit()


def generate_qr(order_id, cart):
    upi_id = os.getenv("UPI_ID", "yourupi@bank")
    name = os.getenv("UPI_NAME", "Vending Machine")
    total = round(sum(item["price"] * item["qty"] for item in cart.values()), 2)
    note = f"Order{order_id}"

    upi_link = (
        f"upi://pay?"
        f"pa={upi_id}&"
        f"pn={name}&"
        f"am={total}&"
        f"cu=INR&"
        f"tn={note}"
    )

    img = qrcode.make(upi_link)
    os.makedirs("static/qr", exist_ok=True)
    file_name = f"qr_order_{order_id}.png"
    file_path = os.path.join("static", "qr", file_name)
    img.save(file_path)
    return file_name


def checkout_order(cart):
    total = round(sum(item["price"] * item["qty"] for item in cart.values()), 2)
    conn = get_connection()

    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orders (total, status) VALUES (%s, %s)",
            (total, "PENDING"),
        )
        order_id = cursor.lastrowid

        for item in cart.values():
            cursor.execute(
                "UPDATE items SET stock = stock - %s WHERE id = %s AND stock >= %s",
                (item["qty"], item["id"], item["qty"]),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return None, None, f"Only limited stock is available for {item['name']}. Please review your cart."

        order_rows = [
            (order_id, item["id"], item["qty"])
            for item in cart.values()
        ]
        cursor.executemany(
            "INSERT INTO order_items (order_id, item_id, quantity) VALUES (%s, %s, %s)",
            order_rows,
        )
        conn.commit()
    except Error:
        conn.rollback()
        raise
    finally:
        conn.close()

    qr_file = generate_qr(order_id, cart)
    return order_id, total, qr_file


def seed_data():
    conn = get_connection()
    cursor = conn.cursor()

    seed_items = [
        ("Coke", 4, 20),
        ("Pepsi", 4, 18),
        ("Sprite", 3, 16),
        ("Fanta", 3, 16),
        ("Water", 1, 40),
        ("Cold Coffee", 6, 12),
        ("Orange Juice", 5, 14),
        ("Lays Chips", 2, 30),
        ("Kurkure", 2, 28),
        ("Nachos", 3, 18),
        ("Popcorn", 3, 22),
        ("Chocolate", 3, 26),
        ("KitKat", 2, 24),
        ("Dairy Milk", 4, 20),
        ("Snickers", 3, 20),
        ("Cookies", 2, 25),
        ("Brownie", 4, 12),
        ("Protein Bar", 5, 10),
        ("Cup Noodles", 4, 15),
        ("Sandwich", 6, 10),
    ]

    for name, price, stock in seed_items:
        cursor.execute("SELECT id FROM items WHERE name = %s LIMIT 1", (name,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                "UPDATE items SET price = %s, stock = %s WHERE id = %s",
                (price, stock, exists[0]),
            )
        else:
            cursor.execute(
                "INSERT INTO items (name, price, stock) VALUES (%s, %s, %s)",
                (name, price, stock),
            )

    conn.commit()
    conn.close()


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "vending_machine_secret_key")


def get_cart():
    if "cart" not in session:
        session["cart"] = {}
    return session["cart"]


@app.route("/")
def index():
    items = get_items()
    cart = get_cart()
    cart_count = sum(item["qty"] for item in cart.values())
    return render_template("index.html", items=items, cart_count=cart_count)


@app.route("/add/<int:item_id>", methods=["POST"])
def add_to_cart(item_id):
    qty = max(int(request.form.get("qty", 1)), 1)
    item = get_item(item_id)
    if not item:
        return redirect(url_for("index"))

    cart = get_cart()
    key = str(item_id)
    current_qty = cart.get(key, {}).get("qty", 0)
    available_stock = item[3]
    safe_qty = min(current_qty + qty, available_stock)

    if safe_qty == 0:
        return redirect(url_for("index"))

    cart[key] = {
        "id": item[0],
        "name": item[1],
        "price": float(item[2]),
        "qty": safe_qty,
        "stock": available_stock,
    }

    session["cart"] = cart
    session.modified = True
    return redirect(url_for("index"))


@app.route("/cart")
def view_cart():
    cart = get_cart()
    items = list(cart.values())
    total = round(sum(item["price"] * item["qty"] for item in items), 2)
    return render_template("cart.html", cart_items=items, total=total)


@app.route("/remove/<int:item_id>", methods=["POST"])
def remove_item(item_id):
    cart = get_cart()
    key = str(item_id)
    if key in cart:
        del cart[key]
        session["cart"] = cart
        session.modified = True
    return redirect(url_for("view_cart"))


@app.route("/checkout", methods=["POST"])
def checkout():
    cart = get_cart()
    if not cart:
        return redirect(url_for("index"))

    order_id, total, qr_or_message = checkout_order(cart)
    if not order_id:
        return render_template("cart.html", cart_items=list(cart.values()), total=total or 0, error=qr_or_message)

    session["cart"] = {}
    session["last_order_id"] = order_id
    session.modified = True

    return render_template(
        "checkout.html",
        order_id=order_id,
        total=total,
        qr_file=qr_or_message,
    )


@app.route("/paid/<int:order_id>", methods=["POST"])
def mark_order_paid(order_id):
    mark_paid(order_id)
    return redirect(url_for("index"))


class VendingTools:
    def list_items(self):
        return get_items()

    def find_item_by_name(self, item_name):
        normalized_query = normalize_text(item_name)
        if not normalized_query:
            return None

        items = self.list_items()
        for item in items:
            if normalize_text(item[1]) == normalized_query:
                return item

        query_terms = normalized_query.split()
        for item in items:
            normalized_name = normalize_text(item[1])
            if normalized_query in normalized_name or all(term in normalized_name for term in query_terms):
                return item

        return None

    def checkout(self, cart):
        return checkout_order(cart)

    def mark_paid(self, order_id):
        mark_paid(order_id)


class VendingAgent:
    def __init__(self):
        self.tools = VendingTools()

    def _extract_add_request(self, text):
        qty_match = re.search(r"(\d+)", text)
        qty = int(qty_match.group(1)) if qty_match else 1

        cleaned = text.lower()
        for token in ["add", "buy", "get", "please", "to", "cart", "x", str(qty)]:
            cleaned = cleaned.replace(token, " ")
        item_name = re.sub(r"\s+", " ", cleaned).strip()
        return item_name, max(qty, 1)

    def _tool_list_items(self):
        rows = self.tools.list_items()
        if not rows:
            return "No items found in vending machine."

        lines = ["Available items:"]
        for item_id, name, price, stock in rows:
            lines.append(f"- {item_id}. {name} | Rs {price} | stock {stock}")
        return "\n".join(lines)

    def _tool_add_to_cart(self, item_name, qty, user_session):
        item = self.tools.find_item_by_name(item_name)
        if not item:
            return f"I could not find '{item_name}'. Try a product name from the menu like Coke or Cold Coffee."

        item_id, name, price, stock = item
        key = str(item_id)

        cart = user_session.get("cart", {})
        current_qty = cart.get(key, {}).get("qty", 0)
        requested = current_qty + qty
        if requested > stock:
            return f"Only {stock} units of {name} are available."

        cart[key] = {
            "id": item_id,
            "name": name,
            "price": float(price),
            "qty": requested,
            "stock": stock,
        }
        user_session["cart"] = cart
        user_session.modified = True
        return f"Added {qty} x {name} to cart."

    def _tool_show_cart(self, user_session):
        cart = user_session.get("cart", {})
        if not cart:
            return "Cart is empty."

        lines = ["Cart summary:"]
        total = 0.0
        for item in cart.values():
            subtotal = item["price"] * item["qty"]
            total += subtotal
            lines.append(
                f"- {item['name']}: {item['qty']} x Rs {item['price']} = Rs {round(subtotal, 2)}"
            )
        lines.append(f"Total = Rs {round(total, 2)}")
        return "\n".join(lines)

    def _tool_checkout(self, user_session):
        cart = user_session.get("cart", {})
        if not cart:
            return "Cart is empty. Add items before checkout."

        order_id, total, qr_or_message = self.tools.checkout(cart)
        if not order_id:
            return qr_or_message

        user_session["cart"] = {}
        user_session["last_order_id"] = order_id
        user_session.modified = True
        return (
            f"Checkout complete. Order ID: {order_id}. Total: Rs {total}. "
            f"QR file: {qr_or_message}. Type 'paid' after payment."
        )

    def _tool_mark_paid(self, user_session):
        last_order_id = user_session.get("last_order_id")
        if not last_order_id:
            return "No recent order found to mark as paid."

        self.tools.mark_paid(last_order_id)
        return f"Order {last_order_id} marked as PAID."

    def respond(self, user_text, user_session):
        try:
            text = user_text.strip().lower()
            if not text:
                return "Please type a command, for example: 'show items', 'add 2 coke', 'cart', 'checkout'."

            if any(k in text for k in ["menu", "show items", "list items", "items"]):
                return self._tool_list_items()

            if any(k in text for k in ["add", "buy", "get"]):
                item_name, qty = self._extract_add_request(text)
                if not item_name:
                    return "Tell me the item name, for example: add 2 Coke"
                return self._tool_add_to_cart(item_name, qty, user_session)

            if "cart" in text:
                return self._tool_show_cart(user_session)

            if "checkout" in text or text == "pay":
                return self._tool_checkout(user_session)

            if text in {"paid", "confirm paid", "mark paid"}:
                return self._tool_mark_paid(user_session)

            if text in {"help", "commands"}:
                return (
                    "Commands:\n"
                    "- show items\n"
                    "- add 2 Coke\n"
                    "- cart\n"
                    "- checkout\n"
                    "- paid\n"
                    "- help"
                )

            return "I did not understand. Type 'help' to see commands."
        except Error as err:
            return (
                "Database connection failed. Check DB_HOST/DB_USER/DB_PASSWORD/DB_NAME in .env. "
                f"MySQL error: {err}"
            )
        except Exception as err:
            return f"I hit an unexpected error: {err}"


agent = VendingAgent()


@app.route("/chat")
def chat():
    return render_template("chatbot.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    reply = agent.respond(user_message, session)
    return jsonify({"reply": reply})


@app.route("/api/cart-count", methods=["GET"])
def api_cart_count():
    cart = get_cart()
    cart_count = sum(item["qty"] for item in cart.values())
    return jsonify({"cart_count": cart_count})


if __name__ == "__main__":
    seed_data()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
