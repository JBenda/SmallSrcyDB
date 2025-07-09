"""convert.py Converts a scryfall bulk json into a sqlite db.

default-cards.json: https://data.scryfall.io/default-cards/default-cards-20250612215456.json
Important: the db should not exists yet!

Usage:
    convert.py collection <DB>
    convert.py search [-c] [-t <location>] <DB> <CARDS>...
    convert.py new <JSON> <DB>
    convert.py update <JSON> <DB>
    convert.py fetch-images <DB>
    convert.py fetch-front-side <JSON> <DB>
    convert.py fetch-sets <JSON> <DB>

Options:
    -c --console  Disable GUI and only produce console output.
    -t --target <location>  Location where to move the cards.
"""

from docopt import docopt
from os import path
import ijson
import json
import sqlite3
import re
import requests
from tqdm import tqdm
from time import sleep
from scipy.optimize import linprog
from functools import reduce
from tabulate import tabulate
from tkinter import ttk
import tkinter as tk
from tkinter.messagebox import Message
from PIL import ImageTk, Image
import io
from functools import partial, reduce

CARD_TABLE = """
create table cards(
    id TEXT primary key,
    cardmarket_id INTEGER,
    layout TEXT not null,
    scryfall_uri TEXT not null,
    uri TEXT not null,
    rarity TEXT not null,
    color_identity TEXT,
    mana_cost TEXT,
    name TEXT not null,
    set_name TEXT not null,
    collector_number TEXT not null,
    legalities TEXT not null,
    digital BOOLEAN not null
)
"""
IMAGE_TABLE = """
CREATE TABLE images(
    id TEXT primary key,
    uri TEXT,
    image BLOB
)
"""
SET_TABLE = """
CREATE TABLE sets(
    id TEXT primary key,
    name TEXT not null
)
"""
COLLECTION_TABLE = """
CREATE TABLE collection(
    id INTEGER primary key,
    card_id REFERENCES cards(id),
    language TEXT not null,
    location REFERENCES locations(id)
)
"""
LOCATION_TABLE = """
CREATE TABLE locations(
    id INTEGER primary key,
    type TEXT not null,
    reference TEXT
)
"""
REG_IGNORE = re.compile(r"(\n|^\s*)", re.MULTILINE)
REG_LOCATION = re.compile(r"(?P<location>\w+)\[(?P<nr>\w+)\]")


class AutocompleteEntry(ttk.Entry):
    """
    Subclass of :class:`ttk.Entry` that features autocompletion.

    To enable autocompletion use :meth:`set_completion_list` to define
    a list of possible strings to hit.
    To cycle through hits use down and up arrow keys.
    """

    def __init__(self, master=None, completevalues=None, **kwargs):
        """
        Create an AutocompleteEntry.

        :param master: master widget
        :type master: widget
        :param completevalues: autocompletion values
        :type completevalues: list
        :param kwargs: keyword arguments passed to the :class:`ttk.Entry` initializer
        """
        ttk.Entry.__init__(self, master, **kwargs)
        self._completion_list = completevalues
        self.set_completion_list(completevalues)
        self._hits = []
        self._hit_index = 0
        self.position = 0

    def set_completion_list(self, completion_list):
        """
        Set a new auto completion list

        :param completion_list: completion values
        :type completion_list: list
        """
        self._completion_list = sorted(completion_list, key=str.lower)  # Work with a sorted list
        self._hits = []
        self._hit_index = 0
        self.position = 0
        self.bind("<KeyRelease>", self.handle_keyrelease)

    def autocomplete(self, delta=0):
        """
        Autocomplete the Entry.

        :param delta: 0, 1 or -1: how to cycle through possible hits
        :type delta: int
        """
        idx_end = self.index("insert")
        query = self.get()
        idx_begin = query.rfind(" ", None, idx_end) + 1
        # collect hits
        _hits = []
        query = query[idx_begin:idx_end]
        try:
            selection_begin = self.index("sel.first")
            if selection_begin >= idx_begin and selection_begin <= idx_end:
                query = query[: selection_begin - idx_begin]
        except tk.TclError:
            pass
        if delta:  # need to delete selection otherwise we would fix the current position
            self.delete(idx_begin + self.position, tk.END)
        else:  # set position to end so selection starts where textentry ended
            self.position = len(query)
        if idx_end - idx_begin > 0:
            for element in self._completion_list:
                if element.startswith(query):
                    _hits.append(element)
        # if we have a new hit list, keep this in mind
        if _hits != self._hits:
            self._hit_index = 0
            self._hits = _hits
        # only allow cycling if we are in a known hit list
        if _hits == self._hits and self._hits:
            self._hit_index = (self._hit_index + delta) % len(self._hits)
        # now finally perform the auto completion
        if self._hits:
            self.delete(idx_begin, tk.END)
            self.insert(idx_begin, self._hits[self._hit_index])
            self.select_range(idx_begin + self.position, tk.END)

    def handle_keyrelease(self, event):
        """
        Event handler for the keyrelease event on this widget.

        :param event: Tkinter event
        """
        if event.keysym == "BackSpace":
            self.delete(self.index(tk.INSERT), tk.END)
            self.position = self.index(tk.END)
        elif event.keysym == "Left":
            if self.position < self.index(tk.END):  # delete the selection
                self.delete(self.position, tk.END)
            else:
                self.position -= 1  # delete one character
                self.delete(self.position, tk.END)
        elif event.keysym == "Right":
            self.position = self.index(tk.END)  # go to end (no selection)
        elif event.keysym == "Down":
            self.autocomplete(1)  # cycle to next hit
        elif event.keysym == "Up":
            self.autocomplete(-1)  # cycle to previous hit
        elif event.keysym == "Return":
            self.handle_return(None)
            return
        # if len(event.keysym) == 1:
        else:
            self.autocomplete()

    def handle_return(self, event):
        """
        Function to bind to the Enter/Return key so if Enter is pressed the selection is cleared.

        :param event: Tkinter event
        """
        self.icursor(tk.END)
        self.selection_clear()

    def config(self, **kwargs):
        """Alias for configure"""
        self.configure(**kwargs)

    def configure(self, **kwargs):
        """Configure widget specific keyword arguments in addition to :class:`ttk.Entry` keyword arguments."""
        if "completevalues" in kwargs:
            self.set_completion_list(kwargs.pop("completevalues"))
        return ttk.Entry.configure(self, **kwargs)

    def cget(self, key):
        """Return value for widget specific keyword arguments"""
        if key == "completevalues":
            return self._completion_list
        return ttk.Entry.cget(self, key)

    def keys(self):
        """Return a list of all resource names of this widget."""
        keys = ttk.Entry.keys(self)
        keys.append("completevalues")
        return keys

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def __getitem__(self, item):
        return self.cget(item)


def center_window(window):
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    x = window.winfo_screenwidth() // 2 - width // 2
    y = window.winfo_screenheight() // 2 - height // 2
    window.geometry(f"{width}x{height}+{x}+{y}")


args = docopt(__doc__, version="convert.py 1.0")
if args["new"]:
    if path.isfile(args["<DB>"]):
        print("'{}' already exists!" % args["<DB>"])
        exit(1)
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    cur.execute(REG_IGNORE.sub("", CARD_TABLE))
    cur.execute(REG_IGNORE.sub("", IMAGE_TABLE))

    with open(args["<JSON>"], "r", encoding="utf-8") as file:
        parser = ijson.items(file, "item")
        data = []
        image_data = []
        with tqdm(total=100000) as pbar:
            for item in parser:
                if item["digital"]:
                    continue
                data.append(
                    (
                        item["id"],
                        item.get("cardmarket_id"),
                        item["layout"],
                        item["scryfall_uri"],
                        item["uri"],
                        item["rarity"],
                        "".join(item.get("color_identity", [])),
                        item.get("mana_cost"),
                        item["name"],
                        item["set"],
                        item["collector_number"],
                        json.dumps(dict(filter(lambda x: x[0] == "commander", item["legalities"].items()))),
                        int(item["digital"]),
                    )
                )
                uri = item.get("image_uris", {}).get("small")
                image_data.append((item["id"], uri))
                if len(data) > 100:
                    try:
                        cur.executemany("INSERT INTO cards VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
                    except sqlite3.IntegrityError:
                        for d in data:
                            for entry in cur.execute("SELECT * FROM cards WHERE id = ?", (d[0],)):
                                print(d)
                                print(entry)
                                print()
                        exit(1)
                    cur.executemany("INSERT INTO images VALUES(?, ?, NULL)", image_data)
                    con.commit()
                    pbar.update(100)
                    data = []
                    image_data = []
elif args["fetch-sets"]:
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    sets = {}
    cur.execute(REG_IGNORE.sub("", SET_TABLE))
    with open(args["<JSON>"], "r", encoding="utf-8") as file:
        parser = ijson.items(file, "item")
        for item in parser:
            if item["digital"]:
                continue
            sets[item["set"]] = item["set_name"]
    cur.executemany("INSERT INTO sets VALUES(?, ?)", sets.items())
    con.commit()

elif args["fetch-images"]:
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    insert = con.cursor()
    (cnt,) = next(cur.execute("SELECT COUNT(*) FROM images WHERE image IS NULL and uri IS NOT NULL"))
    with tqdm(total=cnt) as pbar:
        for id, uri in cur.execute("SELECT id, uri FROM images WHERE image IS NULL AND uri IS NOT NULL"):
            pbar.update(1)
            image = requests.get(uri)
            if image.status_code == 200:
                insert.execute("UPDATE images SET image = ? WHERE id = ?", (image.content, id))
                con.commit()
                sleep(0.01)
            else:
                print("failed to fetch image")
elif args["fetch-front-side"]:
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    with open(args["<JSON>"], "r", encoding="utf-8") as file:
        parser = ijson.items(file, "item")
        with tqdm(
            total=cur.execute("SELECT COUNT(*) FROM cards WHERE layout IN ('transform', 'modal_dfc')").fetchone()[0]
        ) as pbar:
            for item in parser:
                if item["digital"]:
                    continue
                if item["layout"] in ["transform", "modal_dfc"]:
                    uri = item["card_faces"][0]["image_uris"]["small"]
                    if uri != cur.execute("SELECT uri FROM images WHERE id = ?", (item["id"],)).fetchone()[0]:
                        res = requests.get(uri)
                        if res.status_code == 200:
                            cur.execute(
                                "UPDATE images SET uri = ?, image = ? WHERE id = ?", (uri, res.content, item["id"])
                            )
                            con.commit()
                            sleep(0.01)

                    pbar.update(1)


elif args["collection"]:
    print(args["<DB>"])
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = ON")

    ROOT_WIDTH = 480
    ROOT_HEIGHT = 360
    root = tk.Tk()
    root.geometry(
        f"{ROOT_WIDTH}x{ROOT_HEIGHT}"
        f"+{(root.winfo_screenwidth() - ROOT_WIDTH) // 2}+{(root.winfo_screenheight() - ROOT_HEIGHT) // 2}"
    )
    root.title("CollectionMTG")

    def check_table(sql, name):
        table = REG_IGNORE.sub("", sql)
        try:
            (is_state,) = next(cur.execute(f"SELECT sql FROM sqlite_master WHERE name = '{name}'"))
            if is_state != table:
                print(is_state)
                print(table)
                Message(
                    root, title="Table Error", message=f"'{name}' table differs from expected schema", type="ok"
                ).show()
                exit(1)
        except StopIteration:
            cur.execute(table)

    check_table(COLLECTION_TABLE, "collection")
    check_table(LOCATION_TABLE, "locations")

    tabs = ttk.Notebook(root)

    add_card = ttk.Frame(tabs)
    add_card.grid_columnconfigure(3, weight=1)
    search_card = ttk.Frame(tabs)
    tabs.add(add_card, text="Add Cards")
    tabs.add(search_card, text="Search Cards")
    tabs.pack(expand=True, fill="both")

    uuid = tk.StringVar()
    query = tk.StringVar()
    ent_query = AutocompleteEntry(
        add_card,
        textvariable=query,
        width=120,
        completevalues=list(map(lambda x: f"{x[0]}[{x[1]}]", cur.execute("SELECT type, reference FROM locations;"))),
    )

    def delete_last_word(e):
        idx_end = e.widget.index(tk.INSERT)
        idx_begin = e.widget.get().rfind(" ", None, idx_end)
        e.widget.selection_range(idx_begin, idx_end)

    ent_query.bind("<Control-BackSpace>", delete_last_word)
    ent_query.grid(row=1, column=1, columnspan=3)

    FONT_SIZE = 16
    name = tk.StringVar()
    ttk.Label(add_card, textvariable=name, font=("Fira Code", FONT_SIZE)).grid(row=2, column=2, sticky="w", padx=20)
    amount = tk.StringVar()
    ttk.Label(add_card, textvariable=amount, font=("Fira Code", FONT_SIZE)).grid(row=3, column=2, sticky="w", padx=20)
    expansion = tk.StringVar()
    ttk.Label(add_card, textvariable=expansion, font=("Fira Code", FONT_SIZE)).grid(
        row=4, column=2, sticky="w", padx=20
    )
    language = tk.StringVar()
    ttk.Label(add_card, textvariable=language, font=("Fira Code", FONT_SIZE)).grid(row=5, column=2, sticky="w", padx=20)
    location = tk.StringVar()
    ttk.Label(add_card, textvariable=location, font=("Fira Code", FONT_SIZE)).grid(row=6, column=2, sticky="w", padx=20)
    rarity = ttk.Label(add_card, text="", font=("Code Fira", FONT_SIZE))
    rarity.grid(row=7, column=2, sticky="w", padx=20, ipadx=FONT_SIZE * 0.9)

    transactions = []

    def check_target_location(location_str):
        m = REG_LOCATION.match(location_str)
        if m is None:
            print("unable to match location str")
            return
        location_tuple = tuple(m.groupdict().values())
        if (
            m := cur.execute("SELECT id FROM locations WHERE type = ? AND reference = ?", location_tuple).fetchone()
        ) is not None:
            location_id = m[0]
        else:
            if (
                Message(
                    root,
                    title="New Location",
                    message=f"should the location '{location_str}' be created?",
                    type="yesno",
                ).show()
                == "no"
            ):
                return
            root.focus_force()
            (location_id,) = cur.execute(
                "INSERT INTO locations(type, reference) VALUES (?, ?) RETURNING id", location_tuple
            ).fetchone()
            con.commit()
            transactions.append(("location", [location_id]))
            print(f"add location {location_id}")
        return location_id

    def undo_last_transaction(a):
        if tabs.index(tabs.select()) == 0:
            if len(transactions) > 0 and transactions[-1][0] == "collection":
                undo_text = "remove from collection:\n\t" + "\n\t".join(
                    map(
                        lambda x: " ".join(x),
                        cur.execute(
                            "SELECT name, language, set_name, collector_number FROM collection LEFT JOIN cards ON collection.card_id = cards.id WHERE collection.id IN (SELECT value FROM json_each(?))",
                            (json.dumps(transactions[-1][1]),),
                        ),
                    )
                )
                if Message(root, title="Undo", message=undo_text, type="yesno").show() == "yes":
                    cur.execute(
                        "DELETE FROM collection WHERE collection.id IN (SELECT value FROM json_each(?))",
                        (json.dumps(transactions[-1][1]),),
                    )
                    con.commit()
                    print(f"removed collection entries {transactions.pop()[1]}")
            elif len(transactions) > 0 and transactions[-1][0] == "location":
                assert len(transactions[-1][1]) == 1
                undo_text = (
                    "remove collection: "
                    + next(
                        map(
                            lambda x: f"{x[0]}{x[1]}",
                            cur.execute(
                                "SELECT type, reference FROM locations WHERE id = ?", (transactions[-1][1][0],)
                            ),
                        )
                    )
                    + " ?"
                )
                if Message(root, title="Undo", message=undo_text, type="yesno").show() == "yes":
                    cur.execute("DELETE FROM locations WHERE locations.id = ?", (transactions[-1][1][0],))
                    con.commit()
                    print(f"removed location {transactions.pop()[1]}")
        elif tabs.index(tabs.select()) == 1:
            if len(transactions) > 0 and transactions[-1][0] == "move":
                cards, old, current = transactions[-1][1:]
                undo_text = (
                    "move "
                    + ", ".join(
                        map(
                            lambda x: x[0],
                            cur.execute(
                                "SELECT DISTINCT(cards.name) FROM cards JOIN collection ON cards.id = collection.card_id WHERE collection.id IN (SELECT value from json_each(?))",
                                (json.dumps(cards),),
                            ).fetchall(),
                        )
                    )
                    + " from "
                    + "-".join(cur.execute("SELECT type, reference FROM locations WHERE id = ?", (current,)).fetchone())
                    + " to "
                    + "-".join(cur.execute("SELECT type, reference FROM locations WHERE id = ?", (old,)).fetchone())
                )
                if Message(root, title="Undo", message=undo_text, type="yesno").show() == "yes":
                    cur.execute(
                        "UPDATE collection SET location = ? WHERE id IN (SELECT value from json_each(?))",
                        (old, json.dumps(cards)),
                    )
                    con.commit()
                    print(f"moved cards {transactions.pop()[1:]}")

    def search_action(a):
        if tabs.index(tabs.select()) == 0:
            scry_id = uuid.get()
            lan = language.get()
            n = int(amount.get())
            if n <= 0:
                return
            location_id = check_target_location(location.get())
            new_total = n + cur.execute("SELECT COUNT(*) FROM collection WHERE card_id = ?", (scry_id,)).fetchone()[0]
            if new_total > 4:
                if (
                    Message(
                        root,
                        title="Overshoot",
                        message=f"this would add to {new_total} cards, in collection this is more then 4. Do you really want to add thas?",
                        type="yesno",
                    ).show()
                    == "no"
                ):
                    return
                root.focus_force()
            buffer = []
            for i in range(n):
                (new_id,) = cur.execute(
                    "INSERT INTO collection(card_id, language, location) VALUES (?, ?, ?) RETURNING id",
                    (scry_id, lan, location_id),
                ).fetchone()
                buffer.append(new_id)
            name.set("")
            expansion.set("")
            language.set("")
            location.set("")
            rarity.configure(background="SystemButtonFace")
            amount.set("")
            image.configure(image=backface)
            con.commit()
            print(f"add to collection: {buffer}")
            transactions.append(("collection", buffer.copy()))
        elif tabs.index(tabs.select()) == 1:
            new_search_query()

    root.bind("<Return>", search_action)
    root.bind("<Control-z>", undo_last_transaction)

    backface = ImageTk.PhotoImage(Image.open("backface.jpg"))
    image = ttk.Label(add_card, image=backface)
    image.grid(row=2, rowspan=32, column=1, sticky="N")

    def new_query(a, b, c):
        card_set = "??"
        card_set_short = None
        card_nr = None
        card_location = "??"
        card_language = "en"
        card_amount = "1"
        for part in filter(len, map(str.strip, query.get().split(" "))):
            if part in ["en", "de", "jp", "sp", "fr"]:
                card_language = part
                continue
            if (m := REG_LOCATION.match(part)) is not None:
                card_location = (m.group(1), m.group(2))
                continue
            if card_nr is not None and part.isdigit():
                card_amount = int(part)
                continue
            try:
                (card_set,) = next(cur.execute(f"SELECT name FROM sets WHERE id = '{part}'"))
                card_set_short = part
            except StopIteration:
                card_nr = part
        amount.set(str(card_amount))
        expansion.set(card_set)
        language.set(card_language)
        if (
            location_amount := cur.execute(
                "SELECT COUNT(*) FROM collection INNER JOIN locations ON collection.location = locations.id WHERE locations.type = ? AND locations.reference = ?",
                card_location,
            ).fetchone()[0]
        ) > 0:
            location_amount = str(location_amount)
        else:
            location_amount = "(new)"
        location.set(f"{card_location[0]}[{card_location[1]}] {location_amount}")

        if card_set is not None and card_nr is not None:
            try:
                (
                    card_rarity,
                    scryfall_uuid,
                    full_name,
                    img,
                    amount_in_collection,
                ) = next(
                    cur.execute(
                        "SELECT rarity, id, name, (SELECT image FROM images i WHERE i.id = c.id)"
                        " AS image, (SELECT COUNT(*) FROM collection WHERE collection.card_id = c.id) AS amount FROM cards c WHERE set_name = ? AND collector_number = ?",
                        (card_set_short, card_nr),
                    )
                )
                match card_rarity:
                    case "mythic":
                        rarity.configure(background="#DB6A11")
                    case "common":
                        rarity.configure(background="black")
                    case "uncommon":
                        rarity.configure(background="#B7D6E0")
                    case "rare":
                        rarity.configure(background="#C6B991")
                    case _:
                        rarity.configure(background="SystemButtonFace")
                uuid.set(scryfall_uuid)
                name.set(f"{full_name} ({amount_in_collection})")
                if img is not None:
                    img = ImageTk.PhotoImage(Image.open(io.BytesIO(img)))
                    image.configure(image=img)
                    image.image = img
                else:
                    image.configure(image=backface)
            except StopIteration:
                pass
        else:
            uuid.set("")
            name.set("")
            rarity.configure(background="SystemButtonFace")

    query.trace("w", new_query)

    def key_press(event):
        if event.keysym == "Escape":
            if (
                len(transactions)
                and Message(
                    root,
                    title="Close",
                    message="Closing the application will void all possibility to reverse transactions.\n"
                    "Close application?",
                    type="yesno",
                ).show()
                == "no"
            ):
                return
            root.destroy()

    search_query = tk.StringVar()
    search_ui = ttk.Frame(search_card)
    search_ui.pack(side="top", fill="x", expand=True)
    ttk.Entry(search_ui, textvariable=search_query, width=120).grid(row=1, column=1)
    image_canvas = tk.Canvas(search_card)
    image_scorllbar = ttk.Scrollbar(search_card, orient="vertical", command=image_canvas.yview)
    image_frame = ttk.Frame(image_canvas)
    image_frame.bind("<Configure>", lambda e: image_canvas.configure(scrollregion=image_canvas.bbox("all")))
    image_canvas.create_window((0, 0), window=image_frame, anchor="nw")
    image_canvas.configure(yscrollcommand=image_scorllbar.set)

    image_scorllbar.pack(side="right", fill="y")
    image_canvas.pack(side="left", fill="both", expand=True)

    GRID_WIDTH = 3

    def destroy_window(parent, window, e):
        window.destroy()
        parent.focus_force()

    def move_card_between_collections(location_window, scry_id, e):
        migration_window = tk.Toplevel(root)
        collections_raw = cur.execute(
            "SELECT locations.id, locations.type, locations.reference, COUNT(*) "
            "FROM collection LEFT JOIN locations ON locations.id = collection.location "
            "WHERE collection.card_id = ? "
            "GROUP BY locations.id ",
            (scry_id,),
        ).fetchall()

        collections = list(map(lambda x: f"{x[1]}[{x[2]}] x {x[3]}", collections_raw))
        collection_from = tk.StringVar(value=collections[0])
        ttk.OptionMenu(migration_window, collection_from, *collections).grid(row=0, column=1)
        amount = tk.StringVar(value="1")
        ttk.Label(migration_window, text="x").grid(row=0, column=2)
        ttk.Entry(migration_window, textvariable=amount, width=3).grid(row=0, column=3)
        ttk.Label(migration_window, text="=>").grid(row=0, column=4)
        collection_to = tk.StringVar()
        AutocompleteEntry(
            migration_window,
            textvariable=collection_to,
            width=80,
            completevalues=list(
                map(lambda x: f"{x[0]}[{x[1]}]", cur.execute("SELECT type, reference FROM locations;"))
            ),
        ).grid(row=0, column=5)

        def move_card():
            try:
                nr = int(amount.get())
            except ValueError as err:
                print(err)
                return
            idx = collections.index(collection_from.get())
            from_id, from_name, from_ref, from_nr = collections_raw[idx]
            if nr > from_nr:
                Message(
                    migration_window,
                    title="To many elements",
                    message=f"collection only contains {from_nr} crads, but you want to move {nr}",
                    type="ok",
                ).show()
                return
            if (location_id := check_target_location(collection_to.get().strip())) is None:
                return
            if location_id == from_id:
                Message(
                    migration_window,
                    title="Null Action",
                    message="You tried to move a card to the same collection!",
                    type="ok",
                )
                return
            cards_to_move = list(
                map(
                    lambda x: x[0],
                    cur.execute(
                        "SELECT collection.id FROM collection LEFT JOIN locations ON locations.id = collection.location WHERE locations.id = ? AND collection.card_id = ?",
                        (from_id, scry_id),
                    ).fetchmany(nr),
                )
            )
            cur.execute(
                "UPDATE collection SET location = ? WHERE id IN (SELECT value FROM json_each(?))",
                (location_id, json.dumps(cards_to_move)),
            )
            con.commit()
            print(
                cards_to_move,
                f"{from_name}[{from_ref}] ({from_id}) ={nr}=> {collection_to.get().strip()} ({location_id})",
            )
            transactions.append(("move", cards_to_move, from_id, location_id))
            destroy_window(location_window, migration_window, None)
            destroy_window(root, migration_window, None)

        ttk.Button(migration_window, text="Move", command=move_card).grid(row=1, column=1, columnspan=4)
        center_window(migration_window)
        migration_window.bind("<Escape>", partial(destroy_window, location_window, migration_window))
        migration_window.focus_force()

    def show_locations(scry_id, name, e):
        print(isinstance(scry_id, str))
        if isinstance(scry_id, str):
            scry_id = [scry_id]
        location_window = tk.Toplevel(root)
        for i, id in enumerate(scry_id):
            ttk.Label(
                location_window,
                text="\n".join(
                    map(
                        lambda x: f"{x[0]}[{x[1]}] x {x[2]}",
                        cur.execute(
                            "SELECT locations.type, locations.reference, COUNT(*) "
                            "FROM collection LEFT JOIN locations ON locations.id = collection.location "
                            "WHERE collection.card_id = ? "
                            "GROUP BY locations.id ",
                            (id,),
                        ),
                    )
                ),
            ).grid(row=0, column=i)
            image = cur.execute("SELECT image FROM images WHERE id = ?", (id,)).fetchone()
            if image is None or image[0] is None:
                card_image = backface
            else:
                card_image = ImageTk.PhotoImage(Image.open(io.BytesIO(image[0])))
            image_label = ttk.Label(location_window, text="??", image=card_image)
            image_label.image = card_image
            image_label.grid(row=1, column=i)
            image_label.bind("<Button-1>", partial(move_card_between_collections, location_window, id))
        center_window(location_window)

        location_window.bind("<Escape>", partial(destroy_window, root, location_window))
        location_window.focus_force()

    def new_search_query():
        for widget in image_frame.winfo_children():
            widget.destroy()
        search_name = None
        search_legal = None
        search_colorid = None
        search_set = None
        search_id = None
        for part in filter(len, map(str.strip, search_query.get().split(" "))):
            if part.startswith("l:"):
                search_legal = part[2:]
                if search_legal == "c":
                    search_legal = "commander"
            elif part.startswith("id<="):
                search_colorid = set(part[4:].upper())
            elif part.startswith("s:"):
                search_set = part[2:].lower()
            elif part.startswith("i:"):
                search_id = part[2:]
            else:
                if search_name is None:
                    search_name = []
                search_name.append(part.strip())
        query = None

        def append_query(txt):
            nonlocal query
            if query is None:
                query = ""
            else:
                query += " AND"
            query += " " + txt

        if search_legal is not None:
            append_query(f"(SELECT value FROM json_each(legalities) WHERE key = '{search_legal}') = 'legal'")
        if search_colorid is not None:
            append_query(
                f"(SELECT COUNT(value) FROM json_each('{json.dumps(list(search_colorid))}')"
                " WHERE INSTR(cards.color_identity, value)) = LENGTH(cards.color_identity)"
            )
        if search_name is not None:
            for part in search_name:
                append_query(f" cards.name LIKE '%{part}%'")
        if search_set is not None:
            append_query(f" cards.set_name = '{search_set}'")
        if search_id is not None:
            append_query(f" cards.collector_number = '{search_id}'")

        print(query)
        if query is not None:
            for i, (scry_id, name, image, n) in enumerate(
                cur.execute(
                    "SELECT json_group_array(DISTINCT(cards.id)), cards.name, image, COUNT(*)"
                    " FROM cards INNER JOIN collection ON cards.id = collection.card_id"
                    " INNER JOIN images ON images.id == cards.id"
                    f" WHERE {query}"
                    "GROUP BY cards.name"
                )
            ):
                scry_id = json.loads(scry_id)
                ttk.Label(image_frame, text=f"{name}, {n}").grid(row=(i // GRID_WIDTH) * 2, column=i % GRID_WIDTH)
                if image is None:
                    card_image = backface
                else:
                    card_image = ImageTk.PhotoImage(Image.open(io.BytesIO(image)))
                image_label = ttk.Label(image_frame, text=f"{name}, {n}", image=card_image)
                image_label.image = card_image
                image_label.grid(row=(i // GRID_WIDTH) * 2 + 1, column=i % GRID_WIDTH)
                image_label.bind("<Button-1>", partial(show_locations, scry_id, name))

    # search_query.trace("w", new_search_query)

    root.bind("<Escape>", key_press)
    root.mainloop()
elif args["search"]:
    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = ON")

    if (target_location := args["--target"]) is not None:
        if (target_location := REG_LOCATION.match(target_location)) is None:
            print(f"unable to parse location '{target_location}'")
            exit(1)
        if (
            target_location := cur.execute(
                "SELECT id FROM locations WHERE type = ? AND reference = ?", tuple(target_location.groupdict().values())
            ).fetchone()
        ) is None:
            # FIXME: use location new
            print(f"location '{args['--target']}' does not exists")
            exit(1)
        target_location = target_location[0]

    REG_CARD_NAME = re.compile(r"1 (.*)")

    cards = []
    for card in args["<CARDS>"]:
        if path.exists(card):
            with open(card) as file:
                for line in file:
                    cards.append(line)
        else:
            cards.append(card)

    buffer = set()
    for card in cards:
        m = REG_CARD_NAME.match(card.strip())
        if m is None:
            print(f"unable to parse '{card.strip()}' as card name, expect '1 Card Name'")
            exit(1)
        buffer.add(m.group(1))
    assert len(cards) == len(set(cards))
    cards = buffer
    if location is not None:
        cards = list(
            map(
                lambda x: x[0],
                cur.execute(
                    "SELECT value FROM json_each(?) WHERE value NOT IN (SELECT card.name FROM collection coll LEFT JOIN cards card ON coll.card_id = card.id WHERE coll.location = ?)",
                    (json.dumps(cards), location),
                ),
            )
        )
    missing_cards = cur.execute(
        "SELECT json.value from json_each(?) json LEFT JOIN cards ON lower(json.value) = lower(cards.name) "
        "WHERE cards.id IS NULL",
        (json.dumps(cards),),
    ).fetchall()
    if len(missing_cards):
        print("The following card names does not exists! please use the english names")
        for card in missing_cards:
            print("\t", card[0])
        exit(1)

    ids = []
    sets = []
    if target_location is not None:
        # remove cards already in collection
        for (card_in_collection,) in con.execute(
            "SELECT value FROM (SELECT value FROM json_each(?)) WHERE value NOT IN (SELECT cards.name FROM collection RIGHT JOIN cards ON collection.card_id = cards.id WHERE collection.location = ?)",
            (json.dumps(cards), target_location),
        ):
            cards.remove(card_in_collection)
    for id, m in map(
        lambda x: (x[0], set(map(lambda y: cards.index(y), json.loads(x[1])))),
        con.execute(
            "SELECT coll.location, json_group_array(DISTINCT(card.name)) FROM ( "
            "  SELECT id, name FROM ( "
            "    SELECT cards.id, cards.name FROM json_each(?) json JOIN cards ON json.value = cards.name)) "
            "card JOIN collection coll on card.id = coll.card_id "
            "GROUP BY coll.location",
            (json.dumps(cards),),
        ),
    ):
        ids.append(id)
        sets.append(m)
    universe = reduce(set.union, sets)
    assert len(universe) == len(cards)
    c = [1] * len(sets)
    a_ub = []
    b_ub = []
    for e in universe:
        con = [0] * len(sets)
        for j, s in enumerate(sets):
            if e in s:
                con[j] = -1
        a_ub.append(con)
        b_ub.append(-1)
    res = linprog(c, A_ub=a_ub, b_ub=b_ub)

    cards_collected = set()
    hits = []
    for s, _ in sorted(enumerate(res.x), key=lambda x: x[1], reverse=True):
        location, ref = cur.execute("SELECT type, reference FROM locations WHERE id = ?", (ids[s],)).fetchone()
        hits.append([ids[s], f"{location}[{ref}]"])
        for card, card_id, item_id in cur.execute(
            "SELECT DISTINCT(card.name), card.id, coll.id FROM cards card "
            "JOIN collection coll ON card.id = coll.card_id "
            "WHERE coll.location = ? AND card.name IN (SELECT value FROM json_each(?))",
            (ids[s], json.dumps(cards)),
        ):
            idx = cards.index(card)
            if idx not in cards_collected:
                cards_collected.add(cards.index(card))
                hits.append([None, None, card, item_id, card_id])
        if len(cards_collected) == len(universe):
            break
    if args["--console"]:
        print(tabulate(hits, tablefmt="simple"))
        exit(0)

    from tkinter import Tk, ttk, Canvas, StringVar, IntVar
    from PIL import ImageTk, Image
    from functools import partial

    ROOT_WIDTH = 480
    ROOT_HEIGHT = 360
    GRID_WIDTH = 3
    root = Tk()
    root.geometry(
        f"{ROOT_WIDTH}x{ROOT_HEIGHT}"
        f"+{(root.winfo_screenwidth() - ROOT_WIDTH) // 2}+{(root.winfo_screenheight() - ROOT_HEIGHT) // 2}"
    )
    root.title("DeckListMTG")

    backface = ImageTk.PhotoImage(Image.open("backface.jpg"))
    top_bar = ttk.Frame(root)
    top_bar.pack(side="top", fill="x")
    image_canvas = Canvas(root)
    image_scorllbar = ttk.Scrollbar(root, orient="vertical", command=image_canvas.yview)
    image_frame = ttk.Frame(image_canvas)
    image_frame.bind("<Configure>", lambda e: image_canvas.configure(scrollregion=image_canvas.bbox("all")))
    image_canvas.create_window((0, 0), window=image_frame, anchor="nw")
    image_canvas.configure(yscrollcommand=image_scorllbar.set)
    image_scorllbar.pack(side="right", fill="y")
    image_canvas.pack(side="left", fill="both", expand=True)

    locations = list(filter(lambda x: x is not None, map(lambda x: x[1], hits)))
    current_location = StringVar()

    def update_images(value):
        global selected
        location_id, begin, end = offsets[value]
        for widget in image_frame.winfo_children():
            widget.destroy()
        selected = list(map(lambda _: IntVar(), range(begin, len(hits) if end is None else end)))
        for i, (_, _, name, item_id, card_id) in enumerate(hits[begin:end]):
            # skip cards no longer in the table
            if (
                location_id
                != cur.execute(
                    "SELECT locations.id FROM collection JOIN locations "
                    "ON locations.id = collection.location WHERE collection.id = ?",
                    (item_id,),
                ).fetchone()[0]
            ):
                continue
            ttk.Checkbutton(image_frame, text=name, variable=selected[i]).grid(
                row=(i // GRID_WIDTH) * 2, column=i % GRID_WIDTH
            )
            image = None
            if image is None:
                card_image = backface
            else:
                card_image = ImageTk.PhotoImage(Image.open(io.BytesIO(image)))
            image_label = ttk.Label(image_frame, text=name, image=card_image)
            image_label.image = card_image
            image_label.grid(row=(i // GRID_WIDTH) * 2 + 1, column=i % GRID_WIDTH)
            # image_label.bind("<Button-1>", partial(show_locations, card_id, name))

    ttk.OptionMenu(top_bar, current_location, locations[0], *locations, command=update_images).pack(side="left")
    if target_location is not None:

        def move_cards():
            location_id, begin, end = offsets[current_location.get()]
            entries = list(map(lambda x: x[0] + begin, filter(lambda x: x[1].get(), enumerate(selected))))
            print(list(map(lambda x: hits[x][2], entries)), location_id, target_location)
            # FIXME: actually move the cards
            update_images(current_location.get())

        ttk.Button(top_bar, text="Move", command=move_cards).pack(side="left")
    selected = None

    offsets = dict(
        filter(
            lambda x: x[0] is not None,
            map(
                lambda x: (
                    x[1][1],
                    (
                        x[1][0],
                        x[0] + 1,
                        next(
                            map(lambda x: x[0] + 1, filter(lambda x: x[1][0] is not None, enumerate(hits[x[0] + 1 :]))),
                            None,
                        ),
                    ),
                ),
                enumerate(hits),
            ),
        )
    )
    update_images(locations[0])

    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
