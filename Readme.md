## cards.db

```sql
create table cards(
id TEXT primary key,
cardmarket_id INTEGER,
layout TEXT not null,
scryfall_uri TEXT not null,
uri TEXT not null,
color_identity TEXT,
mana_cost TEXT,
name TEXT not null,
set TEXT not null,
collector_number TEXT not null,
legalities TEXT not null,
digital BOOLEAN not null,
);
create table images(
id TEXT primary key,
image BLOB,
);
```
