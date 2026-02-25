import type { Book } from '../types/Book';
import { normalizeSearchText } from '../utils/normalize';

export function searchBooks(books: Book[], query: string): Book[] {
  const trimmedQuery = query.trim();
  if (!trimmedQuery) {
    return books;
  }

  const tokens = trimmedQuery
    .split(/\s+/)
    .map(token => normalizeSearchText(token))
    .filter(token => token.length > 0);

  if (tokens.length === 0) {
    return books;
  }

  return books.filter(book => {
    const searchableText = normalizeSearchText(
      [
        book.title,
        book.author,
        book.narrator,
        book.series,
        book.genre,
      ].join(' ')
    );

    return tokens.every(token => searchableText.includes(token));
  });
}
