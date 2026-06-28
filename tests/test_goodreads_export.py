import csv
import io
import unittest

from scripts.export_skylar_to_goodreads import (
    GOODREADS_FIELDS,
    build_goodreads_row,
    validate_goodreads_rows,
)


class GoodreadsExportTests(unittest.TestCase):
    def test_generated_csv_matches_the_goodreads_contract(self):
        catalog = {
            "example-book": {
                "title": "Example Book",
                "author": "Ada Reader, Bea Writer",
                "year": "2024-09-17",
                "hardcover_rating": "4.25",
            }
        }
        row = build_goodreads_row(
            book_id="example-book",
            rating=4.5,
            review_text='Good, with a "quoted" thought.',
            created_at=None,
            updated_at=None,
            shelf="read",
            catalog=catalog,
            row_num=7,
        )

        self.assertEqual(31, len(GOODREADS_FIELDS))
        self.assertEqual(GOODREADS_FIELDS, list(row))
        self.assertEqual("", row["Book Id"])
        self.assertEqual('=""', row["ISBN"])
        self.assertEqual('=""', row["ISBN13"])
        self.assertEqual("Ada Reader", row["Author"])
        self.assertEqual("Bea Writer", row["Additional Authors"])
        self.assertEqual("2024", row["Year Published"])
        self.assertEqual("Audible Audio", row["Binding"])
        self.assertEqual("read (#7)", row["Bookshelves with positions"])
        validate_goodreads_rows([row])

        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=GOODREADS_FIELDS)
        writer.writeheader()
        writer.writerow(row)
        parsed = next(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(row, parsed)


if __name__ == "__main__":
    unittest.main()
