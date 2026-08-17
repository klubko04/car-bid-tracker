# IAAI CSV schema — raw JSON → analysis CSV

Two sources feed one schema. Apibara is the API; iaai.com is the public site.

```
pull_apibara_01.py           ->  json-raw/apibara_*.json      ]
        |                          stage 1 — spends API quota  ]
        |                                                      ]-> same record shape
pull_iaai_web_01.py          ->  json-raw/iaaiweb_*.json      ]
        |                          stage 1 — no quota, no key  ]
iaai_web_adapt_01.py         ->  json-adapted/adapted_*.json  ]
        |                          stage 1.5 — reshape + enrich
apibara_json2csv_iaai_01.py  ->  csv-raw/*_iaai.csv
        |                          stage 2 — flatten + distance, 57 fields, unfiltered
data_pull_01.py {iaai|copart} ->  csv-cut/*_data.csv
                                   stage 3 — filter + tier + sold period
```

There is **one** flattener and **one** column set. The web source does not get
its own converter; it gets an adapter that reshapes it into the Apibara record
shape. See [The web source](#the-web-source-iaaicom) for why, and for the four
columns it cannot fill.

## Where files live

```
analytics/data/
├── sold/                   lot_sub_status = ended        (auction history)
│   ├── json-raw/{iaai,copart}/
│   ├── json-adapted/{iaai,copart}/
│   ├── csv-raw/{iaai,copart}/
│   └── csv-cut/{iaai,copart}/
└── open/                   lot_sub_status = open | live  (still biddable)
    ├── json-raw/{iaai,copart}/
    ├── json-adapted/{iaai,copart}/
    ├── csv-raw/{iaai,copart}/
    └── csv-cut/{iaai,copart}/
```

Three axes, in the order a file is filed: **bucket** (sold / open) → **layer**
(json-raw / json-adapted / csv-raw / csv-cut) → **platform** (iaai / copart).
The `copart/` folders exist and are empty — placeholders carrying a `.gitkeep`,
since no Copart converter is written yet.

| layer | written by | contents | replaceable? |
|---|---|---|---|
| `json-raw/` | stage 1 | untouched API/site responses | **No** — lots age out of Apibara's rolling ~6-month window, so these are the only lasting copy |
| `json-adapted/` | stage 1.5 | web records reshaped to the Apibara shape | Yes — regenerate free from json-raw |
| `csv-raw/` | stage 2 | every lot in the archive, flattened | Yes — regenerate free |
| `csv-cut/` | stage 3 | filtered + tier/sold_period | Yes — regenerate free |

`json-adapted/` is deliberately **not** inside `json-raw/`: it is derived, and
the raw layer's whole value is being the one thing you cannot recompute.

Only `json-raw/` is irreplaceable, which is the whole reason the layers are
split: back that up and everything else rebuilds without touching the API.

**Filing is data-driven, not filename-driven.** Every archive records its own
`mode` and `platform`, and each record is tagged `_mode` / `_platform` when
loaded, so stages 2 and 3 write into the same bucket *and* platform folder their
input came from — no path juggling, and an archive moved by hand still converts
into the right place. `open` and `live` share the open bucket since both are
still biddable; the filename keeps them apart (`apibara_iaai_open_...` vs
`_live_...`).

`csv-raw/` means *unfiltered by definition*. Running stage 2 **with** filters
writes `_iaai_filtered.csv` instead of `_iaai.csv`, so a filtered run can never
overwrite the canonical extract. Persistent filtering belongs in stage 3.

Bare filenames resolve against both `json-raw/` folders first, then both
`json-adapted/` folders, then the pre-reorganisation trees, so older paths and
commands still work.

Two stage-1 pullers, one downstream:

```bash
# API source — spends quota (100/month)
python analytics/scripts/pull_apibara_01.py iaai open --make Audi --model A5 \
    --year-range 2018-2023 --max-pages 5

# Site source — no key, no quota; --details adds ACV / repair est / title doc
python analytics/scripts/pull_iaai_web_01.py --keyword 2018 Audi A5 --details
python analytics/scripts/iaai_web_adapt_01.py --enrich-from apibara_*.json --audit

# Both converge here
python analytics/scripts/apibara_json2csv_iaai_01.py <archive>.json
python analytics/scripts/data_pull_01.py iaai <archive>.json --tier 2
```

### Reading open lots

The CSV shape is identical across buckets, but the money columns that matter
differ. On `open` lots the `last_sold_*` family and `sold_to_acv` are
structurally empty — nothing has sold yet — and the live figures are
`current_bid_usd` and `buy_now_usd`. `current_bid_usd` is also empty before
bidding opens, so re-pulling an open query closer to the sale date is how bid
movement gets captured; stage 3's newest-wins de-dupe merges repeat pulls of the
same lot automatically.

Stage 2 is documented here. Run
`python analytics/scripts/apibara_json2csv_iaai_01.py --schema` to print the same
mapping from the code — the script's `SCHEMA` list is the single source of truth
and this document is generated from it. Stage 3 adds six more columns, listed at
the bottom.

**IAAI ONLY.** Copart gets its own flattener. Not tidiness — Copart records carry
`details: None`, which deletes `ActualCashValue`, `EstimatedRepairCost`, body
style, storage coordinates and the whole `attributes` block at once.

## The web source (iaai.com)

`pull_iaai_web_01.py` reads iaai.com directly. No API key, no quota, 100 lots per
request against Apibara's 20 — and it sees lots Apibara does not.

### Why an adapter and not a second flattener

Apibara is not an independent database: **it proxies IAAI's own lot view-model**
and unmasks a few fields. Measured on the 8 lots present in both a web pull and
an Apibara pull of the same 2018 A5s:

| block | agreement |
|---|---|
| `details.attributes` | **208/208 keys shared**, 0 web-only, 0 apibara-only |
| `details.vehicle_information` | 10/11 byte-identical |
| `details.vehicle_description` | 14/16 byte-identical |
| `details.sale_information` | 7/10 byte-identical |

A second 57-column flattener would be two copies of one mapping, drifting apart
on every edit. `iaai_web_adapt_01.py` instead rebuilds the derived blocks
Apibara layers on top (`vehicle_specs`, `condition`, `odometer`, `pricing`,
`sale_document`, `seller`, `auction`, `media`) and hands the result to the
existing flattener untouched.

Verify any time with `iaai_web_adapt_01.py --audit`, which diffs every adapted
field against Apibara through the real flattener:

```
web-only                52/57 columns byte-identical
+ --enrich-from         56/57
```

### The four columns the web cannot fill

| column | why | recovered by |
|---|---|---|
| `vin` | masked to 11 chars: `WAUENCF5XJA******` | `--enrich-from` |
| `seller_name` | `Seller`/`ProviderName` blank or `******` | `--enrich-from` |
| `seller_name_masked` | follows from `seller_name` | `--enrich-from` |
| `current_bid_usd` | live bid is an authenticated XHR — the lot page ships `Bidding History … Error Loading Data` | `--enrich-from` |

`--enrich-from` joins on **stock number**, which is `lot_number` in Apibara and
`StockNumber` on the site. Verified on 8/8 overlapping lots: every masked web
prefix matched the Apibara full VIN.

Beware IAAI's two numbers — they are not interchangeable:

```
itemId       46203349    the /VehicleDetail/<id>~US URL id
stockNumber  45704693    Apibara's lot_number, and the join key
```

Enrichment merges **per field, newest non-empty wins**, ordered by each archive's
own `generated_at`. Whole-record newest-wins loses data: Apibara's `seller.name`
is intermittently absent, and on lot 45704693 the 08:52 pull named Progressive
Casualty Insurance while the 14:13 pull did not.

### Seller class survives the masking

The one thing people expect to lose and do not. `attributes.Origin` and
`ProviderType` are populated **65/65** on the web pull, and those are exactly
what `seller_class()` reads:

```
Origin        : {'Insurance': 60, 'Remarketing Vehicles': 5}
ProviderType  : {'INS': 59, 'COR': 2, 'SDS': 1, 'RCC': 1, 'ADJ': 1, 'DLR': 1}
seller_class  : {'insurance': 60, 'other': 4, 'dealer': 1}
```

Only the company *name* needs Apibara. `seller_class()` reads `ProviderType=DLR`
and IAAI's non-insurance origins (`Remarketing Vehicles`, `Repossession`) —
without that a Turo/fleet lot lands in `unknown` web-side while the same lot
lands in `other` from Apibara.

### Listing state — and the flag that does NOT work

The state label lives in `records[].state.state`, parsed from the search page.
It is the only IAAI-native expression of the three site states:

| `state.state` | `AuctionDate` | `InventoryStatus` | `BuyNowIndicator` | n |
|---|---|---|---|---|
| `Auction Not Assigned` | absent | `WC` | false | 56 |
| `Prebid` | present | `RS` | false | 7 |
| `Prebid/BuyNow` | present | `RS` | **true** | 2 |

**`PreBidIndicator` is `true` on all three** and is useless as a discriminator.
The real signal is `InventoryStatus`: `RS` = assigned to a sale, `WC` = in
inventory with no sale scheduled. `BuyNowIndicator` then splits `Prebid` from
`Prebid/BuyNow`.

`Bid Now` (live auction running) did not appear in the reference pull — it is a
narrow window, and Apibara's own `lot_sub_status=Live` returned 0 lots for the
same search at the same moment.

The state is carried into the adapted record as `auction.state` and
`_web_state`. It is **not** a CSV column yet.

### Coverage — why this source exists

Reference pull, `2018 Audi A5`, 2026-08-13:

- iaai.com returned **65 lots**; Apibara's unfiltered 2018-2023 A5 open query returned **14**
- **56 of the 65** were `Auction Not Assigned` — a state Apibara never returns
- Apibara omitted lot `46163678~US`, a 2026-08-25 sale sitting in pre-bid

Apibara's gap was not a filter, a date horizon or a paging artifact: an
unfiltered query returned the same 8/14–8/21 span with `next_cursor: None`,
while a global open query proved lots exist past 8/21.

### Pagination — GET page 1, POST the rest

The search GET renders at most `PageSize=100` and honours **no** page parameter:
`&page=2`, `&CurrentPage=2` and `&pagesize=25` each return byte-identical
page 1. That made the ceiling look absolute for a long time. It is not.

The site's own Knockout pager POSTs the page's `GBPSearchQuery` model back to
`/Search` with `CurrentPage` bumped:

```js
// SearchPage.js
this.CurrentResultsQuery = u.QueryInvoker.Ajax("/Search", "POST", JSON.stringify(e))
```

So `pull_iaai_web_01.py` GETs page 1 — which carries both the model and the
total — then POSTs for each remaining page. The response is the same results
fragment, so every parser works on it unchanged.

Verified on `2018 Mazda Mazda3`, 137 reported:

```
page 1 (GET)   100 rows
page 2 (POST)   37 rows
overlap 0, union 137        <- exactly the reported total
[1/1] 2018 Mazda Mazda3   133 lot(s)   site total: 137   pages: 2
      excluded 4 CA lot(s) (--market us)
```

133 US + 4 Canadian = 137. Two HTTP requests for the whole search.

`--max-pages` (default 20 = 2,000 lots) is a safety cap, not a target — paging
stops as soon as the reported total is covered. **`truncated` changed meaning**:
it now says the pager genuinely did not reach the end (a capped run or a failed
page), rather than being implied by a full first page. Verified with
`--max-pages 1`: 100 of 137, `truncated: True`.

`--year-range` is still useful for keeping each query small and for per-year
cohorts, but is no longer required to get past 100.

Pages are de-duplicated by `item_id` before archiving. In practice they are
disjoint (0 overlap across the 137 Mazda3 lots), but a listing shifting position
between two requests could otherwise repeat.

### What is approximated

| field | difference |
|---|---|
| `sale_document` | Apibara expands per state: web `SALVAGE (Nevada)` → Apibara `SALVAGE - TOTAL LOSS`; web `SALVAGE (Virginia)` → `SALVAGE - BRANDED IF REBUILT`. `title_state` carries the state separately, so no information is lost — the string differs. This is the one column `--audit` still reports as differing (6/8). |
| `run_condition` | `StartsCode` maps `CST` → `RUNS AND DRIVES`, `WST` → `STATIONARY / NO INFORMATION`. In the Apibara corpus 45/1009 `CST` lots are `ENGINE START PROGRAM` instead, and nothing web-side distinguishes them. |

### Sold history is Apibara-only

`last_sold_price_usd`, `last_sold_day`, `last_sold_status` and `sold_to_acv` are
empty on **every** web row. Correct for open lots, but the public site does not
expose sale results at all — so "web for breadth, Apibara for depth" holds for
open inventory, while sold-lot analytics depend entirely on Apibara's quota.

### WhoCanBuy — licence classes

`attributes.WhoCanBuy`, e.g. `DEA,DIS,EXP,REB,SCR`. Which buyer licence types may
bid, and therefore whether you can buy the lot at all.

Populated **only once a sale is assigned** — 9/65 on the reference pull, exactly
the 9 not in `Auction Not Assigned`. Observed sets:

```
5  DEA,DIS,EXP,REB,SCR
2  DEA,DIS,EXP,REB,SCR,TFB
1  DEA,DIS,EXP,LBU,REB,SCR
1  DEA,DIS,EXP,LBU,NAB,PUB,REB,SCR      <- the only lot a public buyer can bid
```

Confident readings: `DEA` dealer, `DIS` dismantler, `EXP` exporter, `REB`
rebuilder, `SCR` scrap, `PUB` public. `LBU`, `NAB` and `TFB` are **unverified** —
they appear on 2, 1 and 2 lots respectively and the meaning has not been
confirmed against IAAI's registration documentation. Do not treat them as known.

The operationally important one is `PUB`: on the reference pull **8 of 9**
assigned lots excluded it, meaning a licence is required. Note the one `PUB` lot
is also the one `Remarketing Vehicles` lot — public-eligible and insurance lots
look like disjoint populations here, but n=1 proves nothing.

The web page puts this under `auctionInformation.biddingInformation.whoCanBuy`
while Apibara puts it in `attributes.WhoCanBuy`; the adapter writes it to the
Apibara path so either source is addressable the same way. It is **not** a CSV
column yet.

### Lane, aisle and branch

Present on both sources and already flattened — `selling_branch` is a CSV column,
and lane/aisle live in the record for anyone who needs them:

```
details.sale_information.Lane          "B - #29"      lane letter + run number
details.sale_information.Aisle         "SG  - 23"     aisle + stall (note 2 spaces)
details.attributes.Lane / Slot         "B" / 29       the same, split
details.attributes.Aisle / Stall       "SG" / 23
details.sale_information.SellingBranch "Bridgeport (PA)"
details.attributes.BranchName/Number   "Bridgeport" / 647
```

These matter on sale day, not for filtering, which is why only `selling_branch`
is promoted to a column.

### Images and video — direct URLs, and higher resolution than Apibara

Yes: the web source yields directly fetchable per-image URLs, and the CSV's
`iaai_image_url_prefix` + `iaai_image_keys` reconstruct them exactly as before.

IAAI's raw key carries the native dimensions, which Apibara's derived thumbs
never expose:

```json
{"k": "46203349~SID~B647~S0~I1~RW2576~H1932~TH0", "w": 2576, "h": 1932, ...}
```

The adapter extracts the `~I<n>` component and rebuilds the compact resizer URL
IAAI itself uses. Verified 7/7 against Apibara on image count. Any width/height
works — measured on key `I1`:

```
width=400&height=300     ->  200 image/jpeg   32,475 bytes
width=845&height=633     ->  200 image/jpeg  139,239 bytes
width=2576&height=1932   ->  200 image/jpeg  854,892 bytes   <- native
```

So `app/image_pipeline.py` can pull full-resolution originals from a web-sourced
row, not just the 845px "large" that Apibara implies.

Video and 360 URLs are deterministic from `SalvageId` (= the `itemId`), and the
adapter emits both into `media.items[]`:

```
video   https://mediaretriever.iaai.com/api/EngineVideoRetriever?partitionKey=<sid>&Tenant=iaai
vr360   https://vis.iaai.com/Home/ThreeSixtyView?keys=SID-<sid>~STP-1~INT-1&iframeview=true
```

Video presence comes from `detail.fields.images.videos` being non-empty —
verified 7/7 against Apibara's `media.has_video`. The `videos[]` entries
themselves have empty `URL` arrays in the static page, so the constructed URL is
the only usable one. `vr360` is emitted into the record but deliberately kept out
of the CSV, unchanged from before.

## IAA Canada — excluded at the pull, by default

`pull_iaai_web_01.py --market` defaults to **`us`**: Canadian lots are dropped
before anything is archived and before any detail page is fetched, keyed on the
item-id suffix (`~US` / `~CA`) that step 1 already carries.

```
[1/1] 2018 Audi A5    65 lot(s)   site total: 67
      excluded 2 CA lot(s) (--market us): 12666581, 12658988
```

This is the one place stage 1 departs from "archive everything untouched", so
it does not depart quietly. Every exclusion is recorded — `counts.market`,
`counts.excluded_by_market`, and `queries[].excluded_by_market` with the lot
numbers — and `search_params.market` states the archive's scope. `--market all`
keeps everything; `--market ca` inverts.

**Absence is judged per lot, not per cohort.** Market is deliberately *not* part
of the cohort key: putting it there would split the history of every US lot on
the day `--market us` was introduced. Instead each snapshot records which
tenants it could have contained, and only a snapshot whose scope includes a
lot's own tenant may call it absent. So after a us-only pull the two Canadian
lots stay `still_listed` — correctly, since that pull had no standing to see
them — while a genuinely departed US lot is still `gone / sold_buy_now`.

Everything below describes what those lots look like when you do pull them
(`--market all` or `ca`), and why the guards exist.

### Why they need their own treatment

IAA runs the US and Canada under one site and **one search**, so an unscoped
`2018 Audi A5` pull returns Canadian lots mixed in without asking. Two of 67 on
one pull:

```
lot 12666581  Toronto North (Ontario)  Imp_3069335~CA  ACV $16,721 CAD  repair $17,798 CAD
lot 12658988  Hamilton (Ontario)       Imp_3061216~CA  ACV $26,117 CAD  repair $18,297 CAD
```

Everything about them differs from a US lot:

| | US | Canada |
|---|---|---|
| item id | `46203349~US` | `Imp_3069335~CA` — prefixed, non-numeric |
| `attributes.Currency` | `USD` | `CAD` |
| `attributes.Market` | `UnitedStates` | `Canada` |
| `BranchState` | `PA` (state code) | `Ontario` (province name) |
| `Zip` | `19405` | `L4A7X4` (postal code) |
| `StorageLocationLatitude/Longitude` | populated | **null** → no `distance_mi` |
| listing state label | `Prebid` etc. | **absent** — no `AddDelWatch` token |

### The silent corruption this caused

`money_num()` strips `$` and `CAD` alike, so **$16,721 CAD was landing in
`acv_usd`** and feeding the max-bid maths as US dollars — roughly a 35%
overstatement that nothing downstream could detect.

Every `*_usd` column now goes through `usd()`, which returns a figure **only**
when the lot is priced in USD. Canadian lots leave `acv_usd`,
`est_repair_usd`, `buy_now_usd`, `current_bid_usd` and `last_sold_price_usd`
empty rather than wrong. Converting was rejected: this pipeline has no business
inventing an FX rate, and a stale one is its own silent error. The native
amounts stay in the archive, and `currency` says what to expect.

`repair_to_acv` and `sold_to_acv` are deliberately **not** guarded — they are
ratios of two same-currency figures, so they stay valid and keep Canadian lots
analysable in relative terms (1.0644 and 0.7006 on the two above).

Stage 2 announces any non-USD lots rather than filing them quietly:

```
*** NON-USD LOTS — *_usd columns left EMPTY for these ***
    Canada / CAD: 2 lot(s)  12666581, 12658988
```

Currency is resolved from `attributes.Currency`, falling back to the **item id
suffix** (`~CA` / `~US`). The fallback matters: a search-only pull has no
`attributes` at all, yet still scrapes a buy-now price off the row, so without
it the guard would leak CAD on exactly the cheap cadence that runs most often.

`app/fees.py` is US-only — Copart/IAAI direct plus three broker stacks — so a
Canadian lot has no correct fee treatment yet, before even reaching import duty
and cross-border transport. Filter on `market` until that exists.

## `lot_url` was broken for every row until 2026-08-15

Built from `lot_number`, it produced `.../VehicleDetail/45704693`, which returns
**HTTP 200** with a `DetailsNotFoundView` shell — dead for US and Canadian lots
alike, and the 200 hides it. The URL needs IAA's **item** id:

```
https://www.iaai.com/VehicleDetail/46203349~US        works
https://www.iaai.com/VehicleDetail/45704693           200, DetailsNotFoundView
```

Now built from `details.attributes.Id`, which both sources carry (Apibara
included), falling back to `_web_item_id` for search-only web rows. All 1,675
existing rows were regenerated.

## CLI values: spaces join, commas separate

Every script follows one rule, so multi-word values never need quoting:

```bash
--model ES 350                    # one value: "ES 350"
--body-style Compact Luxury Car   # one value
--body-style coupe,convertible    # two values
--body-style coupe --body-style "Compact Luxury Car"   # two values
```

This matters because real data contains both: Lexus models are `ES 350` /
`IS 300`, and IAAI body styles include `Compact Luxury Car`. Quoting still works
everywhere; it is simply no longer required.

## Key fields only

57 columns, one per fact. Duplicates were removed after measuring them across the
70-lot reference pull, not by eye — `vehicle_class` was identical to `body_style`
on every row, `key_fob` to `has_key`, `location_display` to `selling_branch`,
while `primary_damage_code`, `acv_raw`, `est_repair_raw`, `body_style_name` and
`runs_and_drives` were each 1:1 with a column that survived.

Three rules decided which side of a 1:1 pair survives:

- **code vs description → description.** `AO`/`LR` mean nothing in a spreadsheet.
- **raw text vs parsed number → number.** `"$29,240 USD"` does not sort or average.
- **boolean vs multi-valued → multi-valued.** `runs_and_drives` looked 1:1 with
  `run_condition` only because this pull contained two of IAAI's three run states;
  the boolean would silently merge `ENGINE START PROGRAM` into `STATIONARY`.

Nothing is lost — the raw JSON archive keeps every field, so restoring a column is
one line in `SCHEMA` plus a re-run, with no API calls.

## Reading the table

- **kind = `raw`** — copied out of the JSON unchanged (only `None`/`""`/`"unknown"`/
  `"******"` normalised to empty).
- **kind = `calc`** — computed by the converter: parsed numbers, ratios,
  classifications, media decomposition.
- **filled** — coverage on the reference pull (70 IAAI Audi S5 lots,
  `apibara_iaai_ended_audi_s5_2018-2023_2025-08-01_2026-08-10`). A low number
  means the field is genuinely sparse in IAAI's data, not that mapping is broken.
  For boolean columns, filled means *present* — `70/70` on a flag says every row
  has a true/false value, not that every row is true.

## Four things that will bite you

**1. `iaai_image_url_prefix` embeds `SalvageId`, which is NOT `lot_number`.**
Lot `45640490` has `SalvageId` `46139013`. Never build image URLs from the lot
number.

**2. Image keys are not contiguous.** A 16-photo lot has keys
`1|2|…|11|115|116|117|118|119` — they jump. They cannot be regenerated from
`image_count`, which is why the full array is stored. Rebuild a URL as:

```
{iaai_image_url_prefix}{key}&width=845&height=633    # large
{iaai_image_url_prefix}{key}&width=400&height=300    # thumb
```

**3. `seller.type` under-reports — which is why it is not a column.** On the
reference pull it said `insurance` for 52 lots and `unknown` for 18, while IAAI's
own `attributes.Origin` said `Insurance` for **all 70**. Several "unknown" lots
name an obvious carrier outright (*Mapfre Usa*, *American Access Casualty Group*,
*Aaa So California*). `seller_class` reads `Origin`/`ProviderType` first and is
the column to trust.

**4. IAAI masks seller identity on some lots** — `SellerType` comes back as
`******` (13 of 70), with `seller.name = "unknown"`. The masking hides *which*
carrier, not *whether* it is one. `seller_name_masked` flags these so they can be
excluded from carrier-level analysis without being miscounted as dealers.

## Deliberately excluded

| Field | Why |
|---|---|
| `media.items[type=vr360]`, `media.has_360` | One fixed URL shape per lot, no analytic signal. |
| `details.auction_information.prebidInformation.*` | Session/UI state — login errors, popup text. Reflects the API client, not the vehicle. |
| `details.attributes.Synonyms`, `EmailTextForShare`, `LocationHours`, `PickupInstructions` | Marketing/branch boilerplate, identical across lots. |
| `$id`, `$values` | .NET serialiser artifacts. |
| codes duplicating descriptions, raw text duplicating parsed numbers | See "Key fields only" above. |

## Columns

### Identity

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `lot_number` | raw | `lot_number` | 70/70 |
| `vin` | raw | `vin` | 70/70 |
| `year` | raw | `year` | 70/70 |
| `make` | raw | `make` | 70/70 |
| `model` | raw | `model` | 70/70 |
| `series` | raw | `details.attributes.Series` | 70/70 |
| `lot_url` | calc | `built from lot_number` | 70/70 |

### Body / specs

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `body_style` | raw | `vehicle_specs.body_style` | 70/70 |
| `engine_raw` | raw | `vehicle_specs.engine.raw` | 70/70 |
| `engine_size_l` | raw | `vehicle_specs.engine.size_l` | 70/70 |
| `engine_hp` | raw | `vehicle_specs.engine.hp` | 69/70 |
| `cylinders` | raw | `details.attributes.Cylinders` | 70/70 |
| `transmission` | raw | `vehicle_specs.transmission` | 70/70 |
| `drive_type` | raw | `vehicle_specs.drive_type` | 70/70 |
| `fuel_type` | raw | `vehicle_specs.fuel_type` | 70/70 |
| `exterior_color` | raw | `vehicle_specs.exterior_color` | 70/70 |
| `options` | raw | `details.vehicle_description.Options` | 69/70 |
| `country_of_origin` | raw | `details.attributes.CountryOfOrigin` | 70/70 |

### `search_keyword`

The iaai.com search that returned the lot, e.g. `2019 Audi A5`. Blank on Apibara
rows, which have no keyword concept.

It exists because **IAAI's keyword search matches loosely and the per-lot `model`
field cannot recover what was searched**: a `2018 Audi RS 5` search came back
6 `RS 3` and 6 `RS 5`, and an `Audi A5` search returns both `A5` and
`A5 SPORTBACK`. Anything that needs to know which search produced a row —
`pull_images_01.py`'s model folder, cohort reasoning in `lot_history_01.py` —
reads this rather than guessing.

### IAA vehicle score

IAA's own automated computer-vision assessment of visible damage severity, taken
from the check-in photos. Two things are easy to get backwards:

- **Higher is better.** 50 is the least damaged, 0 the worst.
- **It is not a percentage** of anything.

Renamed from `vehicle_grade`, which read like a letter grade or a proportion and
is neither. The raw 0-50 value is retained; `iaa_vehicle_score_band` carries
IAA's own band names, verbatim from their score flyer:

| score | band | rows |
|---|---|---|
| 50 | `little damage` | 278 |
| 40-49 | `minor damage` | 534 |
| 30-39 | `moderate damage` | 661 |
| 20-29 | `major damage` | 559 |
| 10-19 | `severe damage` | 441 |
| 0-9 | `non-repairable` | 97 |
| *(blank)* | not assessed | 211 |

Observed values are integers spanning the full 0-50 range, so the scale in the
data matches the published one exactly.

**Blank stays blank rather than collapsing into `non-repairable`.** "Not
assessed" and "assessed as non-repairable" are opposite facts, and 211 of 2,781
rows carry no score — folding them together would invent 211 write-offs.

### Damage grouping

Two derived columns collapse IAAI's ~50 damage strings into the three buckets
that change a rebuild decision. Both are stage-2 columns, so they appear in
`csv-raw` and flow through to `csv-cut`.

| group | values |
|---|---|
| `REAR-SIDE` | Left & Right Side, Left Rear, Left Side, Rear, Right Rear, Right Side, Normal wear & tear, Theft, Hail |
| `FRONT` | Front & Rear, Front End, Front, Left Front, Right Front, Roof |
| `OTHER` | everything else — Flood, Hail, Rollover, All Over, Suspension, Undercarriage, Biohazard, the burn family, … |
| *(blank)* | no damage recorded |

Applied to `primary_damage` and `secondary_damage` alike as
`primary_damage_group` / `secondary_damage_group`. Across 2,217 rows:

```
primary_damage_group    FRONT 1201   REAR-SIDE 635   OTHER 170   blank 211
secondary_damage_group  blank 1410   REAR-SIDE 363   FRONT 262   OTHER 182
```

**Both sides of the comparison are lowercased before lookup**, so casing can
never decide a grouping. The table is written in lowercase and `_norm_damage()`
lowercases the incoming value, then collapses whitespace and tightens `&`
spacing.

That is not cosmetic. **The two fields do not agree with each other**: primary
writes `Front end` / `Left side`, secondary writes `Front End` / `Left Side`,
and secondary carries values primary never does (`Left & Right Side`). A
case-sensitive lookup would have grouped primary correctly and silently dumped
nearly all of secondary into OTHER. `Left  &  Right  Side`, `FRONT END` and
`  Front  ` all resolve correctly.

**Blank stays blank, it does not become OTHER.** "No damage recorded" and
"damage recorded that is neither front nor rear/side" are different facts, and
`secondary_damage` is empty on 1,410 of 2,217 rows — folding those into OTHER
would make the column read as though most lots had exotic damage.

Two placements are worth knowing because they are operator choices, not
geometry: **`Roof` groups with FRONT**, and **`Theft` and `Normal wear & tear`
group with REAR-SIDE**.

A bare **`Front`** was briefly in OTHER — the first spec listed `Front End` and
`Front & Rear` but not `Front` alone. It now groups with FRONT, which moved one
row (FRONT 1200 -> 1201, OTHER 171 -> 170).

### Condition

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `primary_damage` | raw | `condition.primary_damage` | 70/70 |
| `secondary_damage` | raw | `condition.secondary_damage` | 30/70 |
| `secondary_damage_code` | raw | `details.attributes.SecondaryDamageCode ('UK' = inspected, unknown)` | 40/70 |
| `loss_type` | raw | `details.attributes.LossTypeDesc` | 70/70 |
| `run_condition` | raw | `condition.run_condition.value` | 70/70 |
| `has_key` | raw | `condition.has_key` | 70/70 |
| `airbags` | raw | `vehicle_specs.airbags` | 70/70 |
| `iaa_vehicle_score` | raw | `details.attributes.VehicleGrade` | 2570/2781 |
| `iaa_vehicle_score_band` | calc | IAA band name for the score | same |
| `odometer_mi` | raw | `odometer.mi` | 70/70 |

### Title

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `sale_document` | raw | `sale_document.name` | 70/70 |
| `sale_document_group` | raw | `sale_document.sale_document_group` | 70/70 |
| `title_state` | raw | `details.attributes.TitleState` | 70/70 |

### Money

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `last_sold_price_usd` | calc | `pricing.last_sold_price_usd -> number` | 68/70 |
| `current_bid_usd` | calc | `pricing.current_bid_usd -> number` | 51/70 |
| `buy_now_usd` | calc | `pricing.buy_now_usd -> number` | 29/70 |
| `acv_usd` | calc | `details.sale_information.ActualCashValue -> number` | 70/70 |
| `est_repair_usd` | calc | `details.attributes.EstRepairCost (fallback sale_information) -> number` | 48/70 |
| `repair_to_acv` | calc | `est_repair_usd / acv_usd` | 46/70 |
| `sold_to_acv` | calc | `last_sold_price_usd / acv_usd` | 68/70 |

### Auction

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `auction_at` | raw | `auction.auction_at (fallback ad)` | 70/70 |
| `last_sold_day` | raw | `auction.last_sold_day` | 70/70 |
| `last_sold_status` | raw | `auction.last_sold_status` | 70/70 |
| `listing_state` | calc | `_web_state, else derived from InventoryStatus RS/WC + dates` | 100% |
| `timed_close_at` | raw | `auction.timed_end_at (fallback attributes.TimedAuctionCloseDateTime)` | timed lots only |
| `buy_now_close_at` | raw | `auction.buy_now_close_at (fallback attributes.BuyNowCloseDateTime)` | buy-now lots only |
| `buy_now_sold` | raw | `auction.sold_buy_now` | 100% (boolean) |

Three different deadlines, and `auction_at` is only one of them:

```
lot 45662018  auction_at 2026-08-21 16:30   timed_close_at   2026-08-15 01:53   (-6 days)
lot 45704693  auction_at 2026-08-21 13:30   buy_now_close_at 2026-08-21 01:00   (-12 hours)
```

`buy_now_sold` is set by IAAI **while the lot is still listed**. Lot 45250068
carried it on the 08-13 pull and was gone from the site by 08-14 — so a buy-now
sale is catchable on the pull *before* the disappearance, which is the only way
to tell "sold" from "withdrawn" after the fact.

`TimedAuction` is what iaai.com renders as **BID NOW**. The server-side HTML
ships the generic `Pre-Bid` text and the live label is swapped in client-side
once the timed window is open, so the state token in the archive is
`TimedAuction` while the site says BID NOW. `timed_close_at` is what decides
whether that window is currently open.
| `who_can_buy` | raw | `details.attributes.WhoCanBuy` | 1348/1349 apibara, 9/65 web |

`listing_state` speaks IAAI's vocabulary from either source — `Prebid`,
`Prebid/BuyNow`, `TimedAuction`, `Auction Not Assigned`, `Ended`. Web rows carry
the label IAAI printed; Apibara rows derive it from `InventoryStatus` (`RS`
assigned, `WC` inventory-only) plus `TimedAuctionIndicator`.
**`PreBidIndicator` is not used** — it is true on every state. Across the
Apibara corpus: `Ended` 1319, `Prebid` 19, `Prebid/BuyNow` 10,
`Auction Not Assigned` **1** — versus 56 of 65 on a web pull, which is the
coverage gap in one number.

`Bid Now` (a live lane actually running) has still not been observed in any
pull, from either source. It is a minutes-long window during the sale.

`who_can_buy` is empty until a sale is assigned, which is why the web pull fills
it on only the 9 non-`Auction Not Assigned` lots. Frequency across 1,348 Apibara
rows:

```
DIS 1348   REB 1346   SCR 1345   EXP 1343   DEA 1323      <- near-universal
LBU  919   NAB  416   PUB  407   TFB  202   FQO  126      <- discriminating
```

**`PUB` is the one that matters: present on 407/1348 = 30%.** The other ~70%
require a licence. Confident readings: `DEA` dealer, `DIS` dismantler, `EXP`
exporter, `REB` rebuilder, `SCR` scrap, `PUB` public. `LBU`, `NAB`, `TFB` and
`FQO` are **unverified** — do not treat them as known.

### Seller

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `seller_name` | raw | `seller.name (fallback sale_information.Seller)` | 56/70 |
| `seller_name_masked` | calc | `true when SellerType is masked or name is 'unknown'` | 70/70 |
| `seller_class` | calc | `cascade: Origin/ProviderType > seller.type > SellerType > name` | 70/70 |
| `seller_provider_type` | raw | `details.attributes.ProviderType` (raw IAAI code) | 100% |

`seller_provider_type` is carried so the undecoded codes can be studied from the
CSV. `INS` and `DLR` drive `seller_class`; `COR` is confirmed as fleet/
remarketing (Turo Inc, via Apibara's unmasked name); **`SDS`, `RCC` and `ADJ`
are not yet established**. The Apibara corpus is no help — it is 1348 `INS` to 1
`COR`, because almost every pull used `--seller insurance`. Resolving them needs
web pulls, where the mix is visible (`INS` 59, `COR` 2, `SDS` 1, `RCC` 1,
`ADJ` 1, `DLR` 1 on one 65-lot pull).

### Location & distance

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `selling_branch` | raw | `details.sale_information.SellingBranch` | 70/70 |
| `branch_state` | raw | `details.attributes.BranchState` | 70/70 |
| `branch_zip` | raw | `details.attributes.Zip` | 70/70 |
| `branch_lat` | raw | `details.attributes.StorageLocationLatitude` | 70/70 |
| `branch_lng` | raw | `details.attributes.StorageLocationLongitude` | 70/70 |
| `distance_mi` | calc | `haversine(branch_lat/lng -> 98003) x 1.2` | 70/70 |
| `distance_bucket` | calc | `distance_mi rounded UP to the next 250mi` | 70/70 |

### Media

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `image_count` | calc | `len(media.thumbs)` | 70/70 |
| `iaai_image_url_prefix` | calc | `media.thumbs[0] up to '~SID~I'` | 70/70 |
| `iaai_image_keys` | calc | `numeric suffixes of media.thumbs, pipe-joined` | 70/70 |
| `video_count` | calc | `count of media.items[type=video]` | 70/70 |
| `iaai_video_url` | calc | `first media.items[type=video].url` | 46/70 |

Identical from either source — the adapter rebuilds `media.thumbs` / `media.items`
from IAAI's own image keys, verified 7/7 on image count and video presence. The
web source additionally exposes each image's **native** width/height, so these
URLs can be requested at full resolution rather than the 845px "large"; see
[Images and video](#images-and-video--direct-urls-and-higher-resolution-than-apibara).

### Provenance

| CSV column | kind | JSON source | filled |
|---|---|---|---|
| `source_file` | calc | `archive filename this row came from` | 70/70 |
| `pulled_at` | calc | `archive generated_at` | 70/70 |

## Stage 3 — columns added by `data_pull_01.py`

| Column | Source |
|---|---|
| `tier` | `--tier 1\|2\|3`, else per-row `app/tier.py` classification |
| `tier_source` | `cli` / `auto` / `auto:unclassified` |
| `sold_period` | `YYYY-MM` of `last_sold_day` — same convention as the month folder in `app/image_pipeline.py` |

Tier lives here rather than in stage 2 because it is the one field that takes an
operator decision: one archive is one make/model, so `--tier` labels the whole
file, and the automatic classifier returns nothing for a model the want-list does
not yet cover (a 2023 Audi S5 falls outside the table's 2018–2022 window).

### `--history` — nine more columns, from `lot_history_01.py`

Every other column describes a lot at one moment. These describe what changed
between moments, which is where relists and buy-now sales live.

| Column | Meaning |
|---|---|
| `first_seen_at` / `last_seen_at` | bounds of our observation, not of the listing |
| `snapshots` | how many pulls contained this lot |
| `days_listed` | span between first and last sighting |
| `relist_count` | distinct auction **days** seen, minus 1 |
| `auction_at_prior` | the sale date it moved from |
| `buy_now_first_seen` | when a Buy Now first appeared |
| `exit_state` | `still_listed` / `gone` |
| `exit_reason` | `sold_buy_now` / `sold_at_auction` / `sold_on_approval` / `unknown` |
| `exit_price_usd` | what it actually sold for — **Apibara `ended` only** |
| `exit_price_source` | `apibara_ended` / `buy_now_price` — how the price was obtained |
| `declined_approval` | `confirmed` / `inferred` / empty |
| `buy_now_at_relist` | `added` / `kept` / `removed` / `none` |
| `images_first_seen` | first pull where the lot had any photo |
| `acv_first_seen` | first pull where the insurer's ACV was present |
| `assigned_first_seen` | first pull where it left `Auction Not Assigned` |
| `record_versions` | distinct `VersionId` values seen — IAAI's own edit counter |

The three `*_first_seen` columns exist because **an `Auction Not Assigned` lot is
not a finished record.** IAAI back-fills photos and the insurer's ACV after
listing, so a lot can be un-analysable one day and ready the next. Observed on
`2019 Audi A5`: lot 45769760 (Providence RI) listed with **0 images, no ACV, no
repair estimate** — while 25 of its 26 unassigned peers had 16–19 photos.

They are computed from `full` observations only. A search-only pull has neither
images nor ACV, and treating its silence as "no images" would reset the
milestone on every cheap pull.

### One VIN = one lot number, across every relist

Measured on the full Apibara corpus: **1,338 VINs to 1,338 lot numbers, zero
exceptions in either direction.** And the VIN with four auction runs
(`WAUC4CF54JA058014`, runs on 07-08, 07-11, 07-22, 07-25) carries a single
`lot_number` 44948246 throughout.

The stock number identifies the **vehicle**, not the auction run. That is what
makes `(stock_number, AuctionDate)` a valid relist key, and what lets a web row
join to an Apibara record for a lot that has run several times. Holds within
Apibara's ~6-month window; a vehicle re-consigned much later as a fresh intake
could in principle be re-stocked, and there is no evidence either way.

### Pricing a buy-now sale without an API call

Buy-now sales **never reach `lot_sub_status=Ended`** — lot 45250068 was absent
from every ended page across 2026-08-01..08-31 — so waiting for an `ended` pull
to price one waits forever.

It does not need to. A buy-now sale executes *at* the buy-now price, which the
web pull already records, so `exit_price_usd` is filled from the last observed
`buy_now_usd` and `exit_price_source` says `buy_now_price`. Confirmed against
the auction history for 45250068: recorded buy-now $6,875, history reported the
sale at $6,875.

`sold_buy_now` requires corroboration before it is believed. Apibara sets it
True on lots that plainly are not buy-now sales — 2 of 5 ended A5 lots had
`sold_buy_now=True` with `is_buy_now=False`, no `buy_now_usd`, and a $0 sale
price. An exit is only called `sold_buy_now` when the flag is backed by an
actual buy-now price or `is_buy_now`.

### Declined approvals

`Sold on Approval` means the bid was not accepted. Two levels of evidence:

| value | basis |
|---|---|
| `confirmed` | Apibara reported `last_sold_status = 'Sold on Approval'` |
| `inferred` | we watched the lot relist, so the prior run did not stick — decline or no-sale, indistinguishable from outside |

`buy_now_at_relist` then says how the seller responded when the lot came back:
`added`, `kept`, `removed`, or `none`. Worked example:

```
lot 45704693   ran 08-14 at $6,200, Sold on Approval — seller declined
               relisted to 08-21, buy_now_at_relist=added at $7,600
```

$1,400 above the bid they refused. Read together, `declined_approval` +
`buy_now_at_relist` + `buy_now_usd` give the seller's reserve as a number rather
than a guess.

### `CreatedDateTime` is not an arrival date — do not use it as one

The obvious way to ask "how long has this been sitting?" is wrong.
`attributes.CreatedDateTime` is rewritten whenever IAAI updates the record: on
12 of 64 lots it moved between two pulls 19 hours apart, always in lockstep with
`ModifiedDateTime`, while `VersionId` incremented.

```
lot 45107640   08-13 pull: created 7/25/2026  VersionId 1
               08-14 pull: created 8/14/2026  VersionId 2
```

So it is a record-version stamp, not an arrival timestamp, and an "age at
branch" computed from it measures time since the last edit. **`first_seen_at` is
the only honest age**, which is one more reason the history layer exists.
`record_versions` is the honest change counter.

### `--pipeline` — the incoming-supply view

```bash
python analytics/scripts/lot_history_01.py <archive>.json --pipeline
```

Unassigned lots are the majority of inventory (56 of 65 on one A5 pull) and the
only forward-looking part — everything else already has a sale date. The report
gives current completeness plus day-over-day movement:

```
PIPELINE — unassigned inventory as of 2026-08-14T17:17
  32 lot(s) in view: 26 awaiting a sale date, 6 scheduled

  completeness of 26 fully-pulled unassigned lot(s):
      no images          1   45769760
      no ACV             7
      no repair est     15

  since <previous snapshot>:
      newly listed / gained images / gained an ACV / got a sale date
```

It refuses to guess: unassigned lots pulled search-only are counted separately
and excluded from the completeness numbers, with a note to re-run with
`--details`.

### What two days of `2019 Audi A5` showed

The first pull suggested nothing (one no-image lot, no correlation with age).
The second made the pattern legible:

```
2026-08-14   32 lots, 26 unassigned, 1 with no images
2026-08-15   34 lots, 28 unassigned, 3 with no images
             newly listed  2   45881381, 45893726   -> both arrived with 0 images
             gained an ACV 1   45818606
             gained images 0
```

**New arrivals land without photos.** Both lots added on the 15th had zero
images, and 45769760 — first seen on the 14th — still had none ~18 hours later.
So photo back-fill takes longer than a day, which is why the twice-daily cadence
catches state and date changes while imaging is a multi-day watch.

Two supporting correlations, both worth treating as provisional at n=3:

- **Every no-image lot is `Wait Title`** (3/3), and **no lot with a settled
  title document lacks images** (0/14). Photos and title paperwork both land
  after the insurer finalises, so `Wait Title` is the precondition — but not the
  cause, since 17 of 20 `Wait Title` lots do have photos.
- `VersionId 0` (a record IAAI has not yet revised) held 2 of the 4 no-image
  lots.

`record_versions` earns its place here: 45818606 went from 1 version to 2 and
that edit is exactly when its ACV appeared. A version bump is IAAI touching the
record, and `acv_first_seen` / `images_first_seen` say what the touch delivered.

Practical consequence: a lot in its first day or two is not yet analysable —
missing photos, often missing ACV, sometimes `damage=Unknown` and no odometer
(45881381 arrived with an ACV of $21,032 but no damage, mileage or images). Do
not read a thin record as a bad lot; read it as an unfinished one, and re-check
it.

```bash
python analytics/scripts/data_pull_01.py iaai <archive>.json --history --history-cache
python analytics/scripts/lot_history_01.py --all      # standalone, same numbers
```

`--history` **widens the input set** to every archive sharing a search cohort
with the selection, because history over one archive is trivially empty.

Five rules make the difference between real signal and noise:

1. **History is computed before de-dupe.** Stage 3 collapses a lot to one row;
   that is precisely the signal being measured, so the timeline is built from
   every record across every snapshot first, then attached to the survivor.
2. **Absence only counts within a cohort.** Apibara never returns `WC` lots, so
   a web-only lot is "missing" from every Apibara archive by design.
3. **Truncated snapshots cannot prove absence.** A lot missing from a query that
   hit the 100-row ceiling may just be past it. Such snapshots still contribute
   sightings; they just cannot prove a departure.
4. **Unknown scope never licenses an absence claim.** A snapshot may only call a
   lot absent when it demonstrably searched for it — right market, right
   keyword, and the lot's own keyword *known*. This is the rule that produced
   every `exit_state=gone` / `exit_reason=unknown` / `exit_price_usd=` row:

   ```
   lot 45490663   a 2018 A5, present in all 8 of its 2018 pulls including the newest
                  reported `gone` by a 2019-2023 pull that never searched for it
   ```

   Its archives predate keyword tagging, so its keyword read as `None` and the
   check had no grounds to disqualify the later snapshot. Fixed two ways:
   `iaai_web_adapt_01.py` recovers the tag for legacy archives (an archive that
   ran exactly **one** search proves every record came from it), and
   `can_prove_absent` now refuses on an unknown keyword. Afterwards 45490663 is
   `still_listed` with 8 snapshots, and **zero** lots anywhere carry
   `exit_reason=unknown`.

   The general form: an unresolved scope question must fail closed. Failing open
   invents departures, and a phantom departure is worse than a missed one —
   it looks like a data point.
5. **A relist is a change of auction DAY, not timestamp.** Two Apibara pulls
   five hours apart reported lot 45625127 as `2026-08-20 02:30` then
   `2026-08-20 13:30` — a corrected clock, not a reschedule. Comparing full
   timestamps made that a phantom relist; comparing days does not. Relists are
   also computed per cohort, so two sources disagreeing cannot invent one.

Observed on three snapshots of `2018 Audi A5`:

```
lot 45704693  snapshots=3  relists=1  prior=2026-08-14T13:30  still_listed
              ran 08-14, did not sell, rescheduled to 08-21, buy-now added at $7,600
lot 45250068  snapshots=1  relists=0                          gone / sold_buy_now
```

The cache written by `--history-cache` lands in
`data/<bucket>/history/<platform>/<cohort>.json`. It is **derived** — delete it
and the next run rebuilds it identically.

### csv-cut filenames carry a timestamp

A cut is a filtered *view at a moment* — the same filters re-run tomorrow give a
different answer — so stage 3 stamps every output:

```
audi_a5_2018-2023_open_ins_under100k_under3000mi_20260816T103104.csv
```

Applies to `--out` names as well as auto-generated ones. An `--out` that already
contains a `YYYYmmddTHHMMSS` stamp is left alone, so replaying a command from
shell history does not keep appending stamps. Before this, each re-run silently
overwrote the previous cut and two vintages were indistinguishable.

### Filtering on mileage and distance

```bash
--max-odometer 100000     # drop over 100k AND lots with no odometer reading
--max-distance 3000       # drop over 3,000mi AND lots with no branch coordinates
```

Both drop **missing** values as well as over-limit ones. An unknown odometer is
not evidence of low mileage, and a lot with no coordinates cannot be shown to be
in range — keeping either would quietly widen the filter. Each exclusion is
named in the run summary (`odometer unknown`, `distance unknown`) so the cost is
visible rather than assumed.

## Stage 4 — photos for open lots (`pull_images_01.py`)

```bash
python analytics/scripts/pull_images_01.py CUT.csv --year 2018 2019 \
    --primary-damage Rear
python analytics/scripts/pull_images_01.py CUT.csv --where lot_number=45490663
python analytics/scripts/pull_images_01.py CUT.csv --dry-run
```

Takes a csv-cut, applies extra filters, and downloads each lot's photos to

```
images/open/{Make Model}/{FRONT|REAR-SIDE|OTHER}/{platform}/[{year}-][{dist}-]{lot}-{vin}[-{score}][-${buynow}]/{lot}_001.jpg …
```

URLs are rebuilt from `iaai_image_url_prefix` + `iaai_image_keys`, so the CSV is
the only input. `--size` picks dimensions (`thumb` 400x300, `large` 845x633,
**`xl` 1600x1200 default**, `full` 2576x1932) — the resizer honours whatever is
asked. `xl` is the default because damage is what these photos are for and 845px
is too small to judge it; measured ~350–580 KB per image.

This does **not** replace `app/image_pipeline.py`, it complements it. That module
builds the SOLD archive — keyed by VIN, bucketed tier / make-model / distance /
month — because a sold lot's identity is settled and its photos are a permanent
comp. An open lot is the opposite: VIN possibly masked, sale date possibly
nonexistent, and it will be re-pulled as those resolve. So open photos get one
flat folder per lot and no taxonomy. The HTTP download is imported from
`app.image_pipeline._download`, so both paths share one client.

### The folder renames itself when the VIN resolves

```
images/open/Audi A5/FRONT/iaai/2019-1250mi-45866615-WAUENDF56KAxxxxxx-12
images/open/Audi A5/FRONT/iaai/2018-3000mi-45704693-WAUENCF5XJA060484-32-$7600
images/open/Audi RS 5/FRONT/iaai/2019-1250mi-45644589-WUABWCF56KAxxxxxx-05
```

**The top folder is the SEARCH**, not IAAI's per-lot `model`. `--model-folder`
sets it; left off it is derived from the `search_keyword` column with the leading
year stripped (`2019 Audi A5` -> `Audi A5`). Deriving from `model` is wrong twice
over: IAAI writes the trim into that field, so one A5 search yields both `A5` and
`A5 SPORTBACK`; and its keyword search matches loosely — a `2018 Audi RS 5`
search came back **6 RS 3 and 6 RS 5**, which no inference from `model` can undo.

**Year and distance lead** so a listing sorts by age then proximity — the two
coarse filters a human applies before opening anything. Lot and VIN follow as the
identity; Buy Now trails because it is the most volatile.

**Distance is zero-padded to four digits** (`0250mi`, not `250mi`). Unpadded,
string ordering puts `250mi` after `2500mi` and before `3000mi`, and the listing
reads as noise.

Year, distance and Buy Now are all optional, so a lot missing any just has a
shorter name. All three are re-evaluated on every run and the folder renamed in
place — verified on lot 45655991, whose $7,400 Buy Now was **withdrawn** (not
sold) between two pulls; a stale price in a folder name is a lie.

Segments are identified by **shape, never position**: a year is `19xx`/`20xx`, a
**lot and VIN are identified by WIDTH**, which is what makes everything else
unambiguous. Both are fixed in this data — 8 digits and 17 characters on
**2,781 of 2,781 rows, no exceptions** — so they can be lifted out first and the
remaining tokens read from what is left:

```
lot     exactly 8 digits
vin     exactly 17 characters
year    4 digits, 19xx/20xx
dist    digits + "mi"
buy-now starts with "$"
score   whatever 1-2 digit token remains — nothing else is that short
```

The score is therefore written **bare and zero-padded** (`38`, `08`, `50`) rather
than prefixed. Padding matters for the same reason it does on distance: `8` would
sort after `50`.

**An unscored lot gains its segment later.** IAA assigns the score after
processing check-in photos, so an `Auction Not Assigned` lot routinely arrives
without one and gains it days later — the folder is renamed to add the segment
then, and renamed again if IAA re-assesses. Verified for all four transitions:
score appearing, score changing, VIN resolving simultaneously, and migration from
the earlier `score38` form. That is what lets one
parser read every naming generation this tree has had —

```
{lot}-{vin}                      gen 1
{lot}-{vin}-{dist}               gen 2
{lot}-{vin}-{year}-{dist}        gen 3
{year}-{dist}-{lot}-{vin}        gen 4 (current)
```

— which is what made migrating 314 existing folders possible rather than guessed
at. It also keeps masked-VIN detection honest: hand `is_masked_vin()` a whole
tail and `…xxxxxx-2019-2500mi` reads as NOT masked, silently disabling the
VIN-resolution rename.

A folder also **moves between damage groups and model folders** when those
change, rather than forking — `existing_dirs()` searches every group and model,
plus the pre-group and pre-model layouts, so older trees migrate on first
re-pull instead of being stranded.

The masked tail is written as **`xxxxxx`, not `******`** (`--mask-char`). `*` is
legal on ext4 and illegal on Windows, so an asterisk tree cannot cross the WSL
boundary at all. Folders created under the old scheme are migrated on sight —
that is a third rename case alongside the two below.

Masked-ness is detected as **six trailing mask characters**, never as "contains
an x". `X` is a legal VIN character and a legal check digit, so a substring test
would misread real VINs like `WAUXNCF55JA084384` as masked.

When Apibara later supplies
the full VIN — typically once the lot is scheduled and starts appearing in open
pulls — the next image run **renames the folder in place** instead of creating a
second one. Verified end to end on lot 45490663:

```
[1/1] 45490663-WAUENCF55JA******      2 new, 0 present
   ... enrich from an Apibara open pull, regenerate the cut ...
[1/1] 45490663-WAUENCF55JA084384      0 new, 2 present   RENAMED from 45490663-WAUENCF55JA******
```

One lot, one folder, photos preserved, nothing re-downloaded.

The rename only goes **masked -> full**. A later web-only pull, which knows less,
finds the resolved folder and reuses it rather than downgrading the name — also
verified. This works because `lot_number` identifies the folder and is stable
across relists (1 VIN = 1 lot number, 1338/1338).

**`--dry-run` is read-only, and was not always.** Renaming is a side effect of
resolving a folder, so the dry run mutated the tree it was only meant to
describe. `resolve_folder(..., apply=False)` now reports what *would* happen and
touches nothing — verified by hashing the directory listing either side of a dry
run.

Re-running is an incremental sync: a file already on disk with non-zero size is
skipped. Verified — a second pass over the same 26 lots reports
`0 downloaded, 441 already present`. `images/` is gitignored, and a manifest accumulates at
`images/open/manifest_open.csv` recording folder, damage group, model folder,
counts, `renamed_from`,
source CSV and pull time.

### Departed lots are archived, not pruned

```bash
python analytics/scripts/pull_images_01.py CUT.csv --archive-sold
```

Photos of a lot that sold are the most valuable thing in the tree — they are the
comp. Deleting them would be worse than useless, and leaving them under `open/`
makes that tree a lie about what is biddable. So they move to `images/sold/`
keeping the identical shape, which makes an open folder and its sold counterpart
directly comparable:

```
images/open/Audi A4/FRONT/iaai/2018-2000mi-45316975-WAUKMAF44JN015356
images/sold/Audi A4/FRONT/iaai/2018-2000mi-45316975-WAUKMAF44JN015356
```

"Departed" is `exit_state == 'gone'` from `lot_history_01.py`, which is
scope-aware: a lot is only called gone when a later, non-truncated snapshot that
actually covered its market **and** its search keyword failed to contain it. That
matters here because the move is close to destructive — a false positive buries a
live lot in the sold archive.

**A relist reverses it.** `existing_dirs()` searches both buckets, so a lot that
comes back is found under `sold/` and moved to `open/` by the next image run
rather than being downloaded again. Verified round-trip in both directions with
the photos preserved.

### Note on which lots have photos at all

The lots most likely to need a photo pull are the ones Apibara cannot see. On the
test run all four `primary_damage=Rear` lots were `Auction Not Assigned`, so
every VIN was masked and none appeared in a same-day Apibara open pull — which is
the coverage gap doing exactly what it always does. Their folders will rename
themselves once the lots are scheduled.

## Session transcript

`analytics/scripts/build_chat_transcript.py` regenerates
`.cc-discussion/Build analytics pipeline script from test files.md` — a verbatim
copy of the Claude Code session that produced this pipeline, rebuilt from the
session JSONL. Thinking blocks are stored empty (signature only), so they appear
as placeholders rather than being reconstructed.

## Cadence — and why de-dupe prefers the richest record

Step 1 (search) is **one request** and already returns state, auction date and
buy-now for every lot. Step 2 (`--details`) costs one request per lot and adds
ACV, repair estimate, damage codes and deadlines.

```bash
# twice daily — 1 request. State, auction date, buy-now. Feeds history.
python analytics/scripts/pull_iaai_web_01.py --keyword 2018 Audi A5

# once daily — 65 requests. The actual dataset.
python analytics/scripts/pull_iaai_web_01.py --keyword 2018 Audi A5 --details

# once daily — sale RESULTS for anything that concluded since yesterday
python analytics/scripts/pull_apibara_01.py iaai ended --make Audi --model A5 \
    --year-range 2018-2023 --auction-date-range <yesterday> <today> --max-pages 3
```

**Do not use `--details-state assigned` as the daily leg.** It looks like a
free 7x saving and is not: measured on the 56 `Auction Not Assigned` lots of one
pull, `--details` supplies

```
acv > 0       39/56        est_repair    27/56
coords        56/56  -> distance_mi / distance_bucket
image_keys    56/56  -> iaai_image_url_prefix / iaai_image_keys
title_doc     56/56
```

Distance and image URLs are **100% detail-only**, and a search-only row fills
just **9 of 63** columns (`lot_number, vin, year, make, model, lot_url,
listing_state, seller_name_masked, seller_class`). Skipping unassigned lots
blanks damage, odometer, distance and images on 87% of inventory.

`--details-state` is for a fast intra-day refresh of the biddable subset —
8 requests to re-check ACV and deadlines on lots you might bid on today — not
for building the dataset. It takes state names or `assigned` (everything except
`Auction Not Assigned`), expanded at match time so a state IAAI adds later is
included automatically rather than silently skipped, which is how `TimedAuction`
would have been missed.

### Why the Apibara `ended` leg is not optional

iaai.com **never publishes a sale result.** A lot simply disappears, so a
web-only history can say `gone` but never `gone for $7,850`. The daily `ended`
pull over yesterday→today is the only thing that turns

```
exit_state=gone  exit_reason=unknown       exit_price_usd=
```

into

```
exit_state=gone  exit_reason=sold_at_auction   exit_price_usd=21025.0
```

`--history` loads `ended` archives as **price context only** — they resolve
lots, they do not become rows. Without that separation every sold Lexus would
land in an Audi A5 CSV.

An `ended` sighting also outranks absence as evidence: absence supports only
"gone, reason unknown", while a sale record proves the lot concluded and says
for how much. `sold_on_approval` is kept distinct from `sold_at_auction` because
the seller had not accepted — 26% of the sold corpus, and the population most
likely to return as a relist.

A search-only pull produces records marked `_detail_level: "search"`: identity,
state, auction date and buy-now, no attributes. They are adapted, not discarded,
because that is exactly what history needs.

### De-dupe key: the first 11 VIN characters, not the whole VIN

IAAI masks the last 6, so the SAME lot is `WAUENCF5XJA******` from a web pull and
`WAUENCF5XJA060484` once Apibara resolves it. Keying on the full VIN files those
as two different cars:

```
lot 45738201   enriched WAUENCF54JA009708 / web-only WAUENCF54JA******
   full-VIN key -> 2 distinct  => lot emitted TWICE
   11-char key  -> 1 distinct  => lot emitted ONCE
```

The unmasked 11 are the portion both sources always agree on. Current cuts happen
to contain no duplicates only because every archive on disk has been enriched;
the next web-only pull alongside an older enriched archive would have produced
them.

### Merging repeat sightings: static from the richest, volatile from the newest

Neither newest-wins nor richest-wins is right on its own:

| strategy | failure |
|---|---|
| newest-wins | a 1-request search pull carries no ACV, repair estimate or damage codes, so being newest it **blanks them** |
| richest-wins | a full pull from yesterday then shadows today's `listing_state`, `auction_at` and `buy_now_usd` — the row **looks current and is not** |

So `merge_observations()` takes the richest record as the base and overlays the
`auction` and `pricing` blocks plus `_web_state` from the newest. Identity fields
that only ever improve — VIN, seller name — take the better value from either,
so a masked re-pull can never overwrite a resolved VIN.

```
richest (08-16, full)   ACV 17,975   state Prebid          auction 08-18   buy-now -
newest  (08-17, search) ACV -        state Prebid/BuyNow   auction 08-21   buy-now 7,600
merged                  ACV 17,975   state Prebid/BuyNow   auction 08-21   buy-now 7,600
```

`pulled_at` and `source_file` describe **currency** — they name the newest
sighting, because that is what the row's live fields are as of. The static detail
may have been captured earlier in the same cohort; `_merged_from` records every
file involved, and `--history` gives the full span via `first_seen_at` /
`last_seen_at`.

## Distance — computed in stage 2, from IAAI's own coordinates

`distance_mi` and `distance_bucket` are flattener columns, with **no city-table
fallback**. That differs from `app/branch_geo.py`, which resolves
`location.display` through a hand-curated lookup because per-lot coordinates
looked unusable when it was written — `facility.lat` was populated on only 3 of
75 records.

`facility.lat` is still that sparse. But `details.attributes.StorageLocationLatitude/Longitude`
is a different field, and it is populated on **154 of 154 IAAI records across
four independent pulls**, every value inside the continental US
(lat 26.0–47.7, lng −122.3 to −71.0). So stage 2 uses the real coordinates and
nothing else. A lot missing them gets an empty distance and is named in the run
summary — never quietly approximated from a state centroid.

Accuracy is materially better than the city table: on the reference pull the two
methods disagree by one bucket on **22 of 70 lots (31%)**, because the table has
no entry for branches like *Fontana (CA)*, *ACE - Carson (CA)* or *Kansas City
(KS)* — note it holds Kansas City **MO**, not KS — and falls back to a state
centroid. Sanity check on the coordinate path: Seattle 18 mi, Miami-North
3,242 mi.

One consequence to keep in mind: `app/image_pipeline.py` still files photos using
the city table, so a lot's folder can sit in a different bucket than its CSV row.
Same lot, two estimates — the CSV one is closer.

## Relisted lots — the blind spot in every column above

A salvage lot that fails to sell is relisted, often several times. **Nothing in
the CSV shows this**, from either source, and the omission biases the money math
in one direction.

The search payload carries a single `last_sold_*` snapshot — the most recent run
only. `GET /vehicles/{VIN}/history` carries the rest. For VIN
`WAUC4CF54JA058014` (2018 Audi S5, lot 44948246) that endpoint returns four
runs where a normal pull shows one:

```
2026-07-25   $7,850   Sold                 <- the only run a pull reveals
2026-07-22   $7,100   Sold on Approval
2026-07-11   $8,450   Sold
2026-07-08   $6,500   Sold on Approval
```

Read that carefully: the lot kept coming back, so neither earlier `Sold` stuck.
`status` cannot be trusted to mean the car left the building — **the presence of
multiple runs is the evidence, not the status string.** Anyone anchoring a
comp on the $7,850 is reading a number that took three prior failures to reach,
and treating the car as worth more than the market said three times.

Why the corpus cannot see it today: 1,336 distinct VINs across 12 archives,
**0 with more than one lot_number** and 0 with more than one sale date. Each
archive is one model at one moment, and a relist gets a *new* lot number, so
repeat runs simply never land in the same pull.

Three ways to get at it, cheapest first:

| approach | cost | catches |
|---|---|---|
| `last_sold_status = 'Sold on Approval'` | free, already a column | Lots where the seller had not accepted — the population most likely to relist. **351/1349 = 26%** of the corpus |
| Diff repeated sold pulls over time by VIN | free beyond pulls you already make | Only relists occurring while you watch; needs the archives kept, which they are |
| `GET /vehicles/{VIN}/history` | **1 API call per VIN** | Everything, including runs before you started watching |

The endpoint is verified working (`test/test_apibara_history01.py`, raw result
in `test_run/apibara_history_results01.json`) and takes the bare VIN — the
`slug_vin` form 404s. At 100 calls/month it is a shortlist-only tool: resolve
the VIN via the web→Apibara join, then spend one call on each car you are
seriously considering.

### Correction: the web source CAN detect relists going forward

An earlier version of this document claimed the stock number changes on relist,
so iaai.com could not see repeat runs even in principle. **That is wrong.**
Observed by re-pulling the same search 18 hours later:

```
lot 45704693   2026-08-13   Prebid          AuctionDate 2026-08-14 08:30   ActnLnId 186952_2
               2026-08-14   Prebid/BuyNow   AuctionDate 2026-08-21 08:30   ActnLnId (cleared)
```

Same stock number. The sale date moved a week, the lane assignment was cleared,
and a Buy Now was added — the standard response to a lot that did not sell. It
is still listed, so it did not sell on the 14th; contrast lot 45250068, which
sold via Buy Now and simply vanished from the site between the two pulls.

So **a relist keeps the stock number and moves `AuctionDate`**, which makes
longitudinal diffing of web archives a free relist detector:

```python
# same stock_number, different AuctionDate, across two archives = relist event
```

Two honest limits. A single diff cannot prove the lot physically ran and failed
rather than being pulled from the sale beforehand — both look identical from
outside. And this only catches relists that happen *while you are watching*; for
runs predating your first pull, `/vehicles/{VIN}/history` remains the only
source. The web source still cannot supply a VIN, so cross-referencing a relist
to auction history still needs the Apibara join.

What this changes in practice: the third row of the table above ("diff repeated
sold pulls") is not limited to sold pulls or to Apibara. Repeated **web** pulls
of an open search do the same job at zero quota, and they see the 87% of
inventory Apibara never returns.

### Related: `TimedAuction` and the deadline that is not `auction_at`

A fourth listing state, seen when lot 45662018 flipped `Prebid` →
`TimedAuction` between the same two pulls:

```
auction_at       2026-08-21T16:30+00:00      the headline sale date
timed_close_at   2026-08-15T01:53+00:00      when bidding ACTUALLY closes
```

Six days earlier. On a timed (online-only) sale the live date is not the
deadline, so anything scheduling around `auction_at` alone will miss the lot.
`timed_close_at` is a CSV column for exactly this reason; it is empty on
ordinary live-lane lots.

Timed auctions do **not** unlock bid visibility — `highBidAmount` is still `''`
and `bidStatusWarnings` reads `["You are not logged in."]`. Bid data is gated on
login in every state, which is why `current_bid_usd` stays an Apibara-only
column.

### Apibara retention is ~6 months, and paying more does not extend it

Measured on the Basic plan (30k req/month), asking for a full year:

```
requested   2025-08-17 .. 2026-08-17
returned    2026-02-24 .. 2026-08-17      90 lots
```

Confirmed as a wall rather than sparse data: the same query restricted to
`2025-08-17..2026-02-23` returns **0 lots**, and across the entire corpus of
1,445 sold records the earliest `last_sold_day` is **2026-02-16** — exactly six
months before the pull date. The docs say ~10 months; the demo key gave ~5.5;
Basic gives the same ~6. **The paid plan buys rate and volume, not depth.**

Two consequences. A relist older than the window is invisible to
`/vehicles/{VIN}/history` as well. And the json-raw archives really are the only
long-term record — anything older than six months exists nowhere else, which is
what makes the archive-everything rule load-bearing rather than cautious.

A very wide range fails outright rather than clamping: `2025-01-01..2026-02-23`
with no model filter returned **HTTP 502 `Vehicle API request failed`**. Keep
requested ranges inside the retention window.

## Copart differences (for the future `apibara_json2csv_copart_01.py`)

Measured on a 20-lot Copart pull:

| Column group | IAAI | Copart |
|---|---|---|
| `details` block entirely | present | **`None` on 20/20** |
| `acv_usd`, `est_repair_usd` | 70/70 | **absent** — no ACV, no repair estimate |
| `body_style` | 70/70 | **0/20** — null on every record |
| `branch_lat` / `branch_lng` | 70/70 | absent (use `facility.*`, sparse) — so Copart distance must fall back to the city table |
| `seller_class` inputs | `Origin`/`ProviderType` | absent; but `seller.type` is clean (`non_insurance` populated) |
| engine | `"3.0L V-6 DI, DOHC, VVT, turbo, 354HP"` | `"3.0L 6"` — no layout, no HP |
| damage vocabulary | positional (`Left rear`, `Front & rear`) | coarse (`Rear end`, `Side`, `All over`) |

The practical consequence: **IAAI lots are self-pricing and Copart lots are not.**
`acv_usd` is the insurer's own clean-value figure and `est_repair_usd` its repair
estimate, so an IAAI row carries everything the max-bid calculation needs without
a MarketCheck call. A Copart row needs an external clean value.
