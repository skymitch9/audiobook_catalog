/**
 * Formatting utilities for displaying durations and other data.
 * 
 * These functions convert raw data (like minutes) into human-readable formats.
 */

/**
 * Formats duration in minutes to "Xh Ym" format.
 * 
 * @param minutes - Duration in minutes
 * @returns Formatted duration string (e.g., "5h 30m", "2h 0m", "45m")
 * 
 * @example
 * formatDuration(330) // "5h 30m"
 * formatDuration(120) // "2h 0m"
 * formatDuration(45) // "45m"
 */
export function formatDuration(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  
  if (hours === 0) {
    return `${mins}m`;
  }
  
  return `${hours}h ${mins}m`;
}

/**
 * Formats duration in minutes to "X days Y hours" format.
 * 
 * Useful for displaying total listening time in statistics.
 * 
 * @param minutes - Duration in minutes
 * @returns Formatted duration string (e.g., "2 days 5 hours", "1 day 0 hours", "5 hours")
 * 
 * @example
 * formatDurationDays(3000) // "2 days 5 hours"
 * formatDurationDays(1440) // "1 day 0 hours"
 * formatDurationDays(300) // "5 hours"
 */
export function formatDurationDays(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  
  if (days === 0) {
    return `${hours} ${hours === 1 ? 'hour' : 'hours'}`;
  }
  
  const dayStr = `${days} ${days === 1 ? 'day' : 'days'}`;
  const hourStr = `${remainingHours} ${remainingHours === 1 ? 'hour' : 'hours'}`;
  
  return `${dayStr} ${hourStr}`;
}
