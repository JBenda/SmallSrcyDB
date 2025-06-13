"""convert.py Converts a scryfall bulk json into a sqlite db.

default-cards.json: https://data.scryfall.io/default-cards/default-cards-20250612215456.json
Important: the db should not exists yet!

Usage:
    convert.py collection <DB>
    convert.py new <JSON> <DB>
    convert.py fetch-images <DB>
    convert.py fetch-sets <JSON> <DB>
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
elif args["collection"]:
    from tkinter import Tk, ttk, StringVar, Canvas
    from tkinter.messagebox import Message
    from PIL import ImageTk, Image
    import io
    from functools import partial

    con = sqlite3.connect(args["<DB>"])
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = ON")

    ROOT_WIDTH = 480
    ROOT_HEIGHT = 360
    root = Tk()
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

    uuid = StringVar()
    query = StringVar()
    ttk.Entry(add_card, textvariable=query, width=120).grid(row=1, column=1, columnspan=3)

    FONT_SIZE = 16
    name = StringVar()
    ttk.Label(add_card, textvariable=name, font=("Fira Code", FONT_SIZE)).grid(row=2, column=2, sticky="w", padx=20)
    amount = StringVar()
    ttk.Label(add_card, textvariable=amount, font=("Fira Code", FONT_SIZE)).grid(row=3, column=2, sticky="w", padx=20)
    expansion = StringVar()
    ttk.Label(add_card, textvariable=expansion, font=("Fira Code", FONT_SIZE)).grid(
        row=4, column=2, sticky="w", padx=20
    )
    language = StringVar()
    ttk.Label(add_card, textvariable=language, font=("Fira Code", FONT_SIZE)).grid(row=5, column=2, sticky="w", padx=20)
    location = StringVar()
    ttk.Label(add_card, textvariable=location, font=("Fira Code", FONT_SIZE)).grid(row=6, column=2, sticky="w", padx=20)
    rarity = ttk.Label(add_card, text="", font=("Code Fira", FONT_SIZE))
    rarity.grid(row=7, column=2, sticky="w", padx=20, ipadx=FONT_SIZE * 0.9)

    REG_LOCATION = re.compile(r"(?P<location>\w+)\[(?P<nr>\w+)\]")

    def add_to_collection(a):
        if tabs.index(tabs.select()) == 0:
            scry_id = uuid.get()
            lan = language.get()
            n = int(amount.get())
            if n <= 0:
                return
            m = REG_LOCATION.match(location.get())
            if m is None:
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
                        message=f"should the location '{location.get()}' be created?",
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
            cur.execute(
                "INSERT INTO collection(card_id, language, location) VALUES (?, ?, ?)", (scry_id, lan, location_id)
            )
            name.set("")
            expansion.set("")
            language.set("")
            location.set("")
            rarity.configure(background="SystemButtonFace")
            amount.set("")
            image.configure(image=backface)
            con.commit()

    root.bind("<Return>", add_to_collection)

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
            if part in ["en", "de", "jp"]:
                card_language = part
                continue
            if (m := REG_LOCATION.match(part)) is not None:
                card_location = m.group()
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
        location.set(card_location)

        if card_set is not None and card_nr is not None:
            try:
                (
                    card_rarity,
                    scryfall_uuid,
                    full_name,
                    img,
                ) = next(
                    cur.execute(
                        "SELECT rarity, id, name, (SELECT image FROM images i WHERE i.id = c.id)"
                        " AS image FROM cards c WHERE set_name = ? AND collector_number = ?",
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
                name.set(full_name)
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
            root.destroy()

    search_query = StringVar()
    search_ui = ttk.Frame(search_card)
    search_ui.pack(side="top", fill="x", expand=True)
    ttk.Entry(search_ui, textvariable=search_query, width=120).grid(row=1, column=1)
    image_canvas = Canvas(search_card)
    image_scorllbar = ttk.Scrollbar(search_card, orient="vertical", command=image_canvas.yview)
    image_frame = ttk.Frame(image_canvas)
    image_frame.bind("<Configure>", lambda e: image_canvas.configure(scrollregion=image_canvas.bbox("all")))
    image_canvas.create_window((0, 0), window=image_frame, anchor="nw")
    image_canvas.configure(yscrollcommand=image_scorllbar.set)

    image_scorllbar.pack(side="right", fill="y")
    image_canvas.pack(side="left", fill="both", expand=True)

    GRID_WIDTH = 3

    def show_locations(scry_id, name, e):
        text = []
        for type, ref, n in cur.execute(
            "SELECT locations.type, locations.reference, COUNT(*) FROM collection LEFT JOIN locations ON locations.id = collection.location WHERE collection.card_id = ? GROUP BY locations.id",
            (scry_id,)
        ):
            text.append(f"{type}[{ref}] x {n}")
        Message(root, title="Card Locations:", message="\n".join(text), type="ok").show()
        root.focus_force()

    def new_search_query(a, b, c):
        for widget in image_frame.winfo_children():
            widget.destroy()
        q = search_query.get().strip()
        for i, (scry_id, name, image, n) in enumerate(
            cur.execute(
                "SELECT cards.id, cards.name, image, COUNT(*)"
                " FROM cards INNER JOIN collection ON cards.id = collection.card_id"
                " INNER JOIN images ON images.id == cards.id"
                f" WHERE name LIKE '%{q}%' GROUP BY cards.id"
            )
        ):
            ttk.Label(image_frame, text=f"{name}, {n}").grid(row=(i // GRID_WIDTH) * 2, column=i % GRID_WIDTH)
            if image is None:
                card_image = backface
            else:
                card_image = ImageTk.PhotoImage(Image.open(io.BytesIO(image)))
            image_label = ttk.Label(image_frame, text=f"{name}, {n}", image=card_image)
            image_label.image = card_image
            image_label.grid(row=(i // GRID_WIDTH) * 2 + 1, column=i % GRID_WIDTH)
            image_label.bind("<Button-1>", partial(show_locations, scry_id, name))

    search_query.trace("w", new_search_query)

    root.bind("<Escape>", key_press)
    root.mainloop()
