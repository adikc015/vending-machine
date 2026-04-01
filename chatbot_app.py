import os
import re
from dataclasses import dataclass, field

import mysql.connector
from mysql.connector import Error
import qrcode
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AgentState:
    cart: dict = field(default_factory=dict)
    last_order_id: int | None = None


class VendingTools:
    def __init__(self):
        self.db_config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "user": os.getenv("DB_USER", "root"),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "vending_machine"),
        }

    def get_connection(self):
        return mysql.connector.connect(**self.db_config)

    def list_items(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, price, stock FROM items ORDER BY id")
        rows = cursor.fetchall()
        conn.close()
        return rows

    def find_item_by_name(self, item_name):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, price, stock FROM items WHERE LOWER(name)=LOWER(%s) LIMIT 1",
            (item_name.strip(),),
        )
        row = cursor.fetchone()
        conn.close()
        return row

    def create_order(self, total):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO orders (total, status) VALUES (%s, %s)",
            (total, "PENDING"),
        )
        conn.commit()
        order_id = cursor.lastrowid
        conn.close()
        return order_id

    def add_order_item(self, order_id, item_id, qty):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO order_items (order_id, item_id, quantity) VALUES (%s, %s, %s)",
            (order_id, item_id, qty),
        )
        conn.commit()
        conn.close()

    def update_stock(self, item_id, qty):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE items SET stock = stock - %s WHERE id = %s AND stock >= %s",
            (qty, item_id, qty),
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        return updated > 0

    def mark_paid(self, order_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = 'PAID' WHERE id = %s", (order_id,))
        conn.commit()
        conn.close()

    def generate_qr(self, order_id, total):
        upi_id = os.getenv("UPI_ID", "yourupi@bank")
        name = os.getenv("UPI_NAME", "Vending Machine")
        note = f"Order{order_id}"
        upi_link = (
            f"upi://pay?"
            f"pa={upi_id}&"
            f"pn={name}&"
            f"am={round(total, 2)}&"
            f"cu=INR&"
            f"tn={note}"
        )

        os.makedirs("static/qr", exist_ok=True)
        file_name = f"qr_order_{order_id}.png"
        file_path = os.path.join("static", "qr", file_name)
        qrcode.make(upi_link).save(file_path)
        return file_path


class VendingAgent:
    def __init__(self):
        self.tools = VendingTools()
        self.state = AgentState()

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

    def _tool_add_to_cart(self, item_name, qty):
        item = self.tools.find_item_by_name(item_name)
        if not item:
            return f"I could not find '{item_name}'. Try exact product name from the menu."

        item_id, name, price, stock = item
        key = str(item_id)
        current_qty = self.state.cart.get(key, {}).get("qty", 0)
        requested = current_qty + qty
        if requested > stock:
            return f"Only {stock} units of {name} are available."

        self.state.cart[key] = {
            "id": item_id,
            "name": name,
            "price": float(price),
            "qty": requested,
        }
        return f"Added {qty} x {name} to cart."

    def _tool_show_cart(self):
        if not self.state.cart:
            return "Cart is empty."
        lines = ["Cart summary:"]
        total = 0.0
        for item in self.state.cart.values():
            subtotal = item["price"] * item["qty"]
            total += subtotal
            lines.append(f"- {item['name']}: {item['qty']} x Rs {item['price']} = Rs {round(subtotal, 2)}")
        lines.append(f"Total = Rs {round(total, 2)}")
        return "\n".join(lines)

    def _tool_checkout(self):
        if not self.state.cart:
            return "Cart is empty. Add items before checkout."

        total = round(sum(i["price"] * i["qty"] for i in self.state.cart.values()), 2)
        order_id = self.tools.create_order(total)

        for item in self.state.cart.values():
            stock_updated = self.tools.update_stock(item["id"], item["qty"])
            if not stock_updated:
                return f"Checkout failed due to stock change for {item['name']}. Please review cart."
            self.tools.add_order_item(order_id, item["id"], item["qty"])

        qr_path = self.tools.generate_qr(order_id, total)
        self.state.last_order_id = order_id
        self.state.cart = {}
        return (
            f"Checkout complete. Order ID: {order_id}. Total: Rs {total}. "
            f"QR saved at: {qr_path}. Type 'paid' after payment."
        )

    def _tool_mark_paid(self):
        if not self.state.last_order_id:
            return "No recent order found to mark as paid."
        self.tools.mark_paid(self.state.last_order_id)
        return f"Order {self.state.last_order_id} marked as PAID."

    def respond(self, user_text):
        try:
            text = user_text.strip().lower()
            if not text:
                return "Please type a command, for example: 'show items', 'add 2 coke', 'cart', 'checkout'."

            if any(k in text for k in ["menu", "show items", "list items", "items"]):
                return self._tool_list_items()

            if any(k in text for k in ["add", "buy", "get"]):
                item_name, qty = self._extract_add_request(text)
                if not item_name:
                    return "Tell me item name, for example: add 2 Coke"
                return self._tool_add_to_cart(item_name, qty)

            if "cart" in text:
                return self._tool_show_cart()

            if "checkout" in text or "pay" in text:
                return self._tool_checkout()

            if text in {"paid", "confirm paid", "mark paid"}:
                return self._tool_mark_paid()

            if text in {"help", "commands"}:
                return (
                    "Commands:\n"
                    "- show items\n"
                    "- add 2 Coke\n"
                    "- cart\n"
                    "- checkout\n"
                    "- paid\n"
                    "- exit"
                )

            return "I did not understand. Type 'help' to see commands."
        except Error as err:
            return (
                "Database connection failed. Check DB_HOST/DB_USER/DB_PASSWORD/DB_NAME in .env. "
                f"MySQL error: {err}"
            )
        except Exception as err:
            return f"I hit an unexpected error: {err}"


def run_cli():
    agent = VendingAgent()
    print("Vending Agent AI ready. Type 'help' for commands, 'exit' to quit.")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            print("Agent: Bye.")
            break
        reply = agent.respond(user_input)
        print(f"Agent: {reply}")


if __name__ == "__main__":
    run_cli()
