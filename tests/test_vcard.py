from familia_mcp import vcard


def test_parse_folded_grouped_and_typed():
    text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:José María\r\n  de la Fuente\r\nitem1.TEL;TYPE=\"CELL,VOICE\":+34 6\r\n"
            " 00 00 00 00\r\nTEL;HOME:91 000 00 00\r\nEMAIL:jm@example.org\r\nBDAY:19800102\r\n"
            "ADR;TYPE=HOME:;;C/ Mayor\\, 1;Madrid;;28001;España\r\nEND:VCARD\r\n")
    c = vcard.parse(text)
    assert c.name == "José María de la Fuente"
    assert c.phones == [{"number": "+34 600 00 00 00", "type": "cell"}, {"number": "91 000 00 00", "type": "home"}]
    assert c.emails == [{"email": "jm@example.org"}] and c.birthday == "1980-01-02"
    assert c.address == "C/ Mayor, 1, Madrid, 28001, España"


def test_duplicate_phones_and_emails_collapse():
    c = vcard.parse("BEGIN:VCARD\r\nFN:Hugo\r\nTEL;TYPE=CELL:+34607000000\r\nTEL:607 00 00 00\r\n"
                    "EMAIL:Y@ex.org\r\nEMAIL:y@ex.org\r\nEND:VCARD")
    assert c.phones == [{"number": "+34607000000", "type": "cell"}] and c.emails == [{"email": "Y@ex.org"}]


def test_name_falls_back_to_n_then_org():
    assert vcard.parse("BEGIN:VCARD\r\nN:López;Luis;;;\r\nTEL:1\r\nEND:VCARD").name == "Luis López"
    assert vcard.parse("BEGIN:VCARD\r\nORG:Farmacia Sol\r\nTEL:1\r\nEND:VCARD").name == "Farmacia Sol"
    assert vcard.parse("BEGIN:VCARD\r\nTEL:1\r\nEND:VCARD") is None


def test_birthdays():
    assert vcard.parse_birthday("1980-01-02") == "1980-01-02"
    assert vcard.parse_birthday("--0531") == "--05-31"
    assert vcard.parse_birthday("1980-01-02T00:00:00Z") == "1980-01-02"
    assert vcard.parse_birthday("2023-02-30") is None


def test_build_escapes_folds_and_roundtrips():
    long_note = "Ñandú " * 30 + "fin"
    c = vcard.Contact(name="Pedro; el, fontanero", phones=[{"number": "+34 600 000 000", "type": "cell"}],
                      emails=[{"email": "p@example.org", "type": "work"}], birthday="--05-31",
                      organization="Fontanería López", address="C/ Sol 3, Madrid", note=long_note)
    text = vcard.build(c)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "FN:Pedro\\; el\\, fontanero" in text
    assert "BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-31" in text
    back = vcard.parse(text)
    assert back.name == c.name and back.note == long_note and back.birthday == "--05-31"
    assert back.phones == c.phones and back.emails == c.emails and back.organization == c.organization
