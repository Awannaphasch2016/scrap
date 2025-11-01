import requests
from bs4 import BeautifulSoup
from typing import Dict, Optional
import json


def scrape_first_blog_post(url: str = "https://aswathdamodaran.blogspot.com/") -> Optional[Dict]:
    """
    Scrapes the first blog post from Aswath Damodaran's blog.

    Args:
        url: The blog URL (default: Aswath Damodaran's blog)

    Returns:
        Dictionary containing post data or None if scraping fails
    """
    try:
        # Fetch the page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')

        # Find the first post
        first_post = soup.find('div', class_='post')

        if not first_post:
            print("No post found on the page")
            return None

        # Extract post details
        post_data = {}

        # Get title
        title_element = first_post.find('h3', class_='post-title')
        if title_element:
            title_link = title_element.find('a')
            post_data['title'] = title_link.get_text(strip=True) if title_link else title_element.get_text(strip=True)
            post_data['url'] = title_link['href'] if title_link else None

        # Get date
        date_element = first_post.find_previous('h2', class_='date-header')
        if date_element:
            post_data['date'] = date_element.get_text(strip=True)

        # Get post body content
        body_element = first_post.find('div', class_='post-body')
        if body_element:
            post_data['content'] = body_element.get_text(strip=True)
            post_data['html_content'] = str(body_element)

        # Get labels/categories
        labels = []
        footer = first_post.find('div', class_='post-footer')
        if footer:
            label_elements = footer.find_all('a', rel='tag')
            labels = [label.get_text(strip=True) for label in label_elements]
        post_data['labels'] = labels

        # Get author if available
        author_element = first_post.find('span', class_='post-author')
        if author_element:
            post_data['author'] = author_element.get_text(strip=True)

        return post_data

    except requests.RequestException as e:
        print(f"Error fetching the page: {e}")
        return None
    except Exception as e:
        print(f"Error parsing the page: {e}")
        return None


def main():
    """Main function to run the scraper"""
    print("Scraping Aswath Damodaran's blog...")
    print("-" * 60)

    post_data = scrape_first_blog_post()

    if post_data:
        print(f"\nTitle: {post_data.get('title', 'N/A')}")
        print(f"\nDate: {post_data.get('date', 'N/A')}")
        print(f"\nURL: {post_data.get('url', 'N/A')}")
        print(f"\nLabels: {', '.join(post_data.get('labels', []))}")
        print(f"\nContent Preview (first 500 chars):")
        print(post_data.get('content', 'N/A')[:500] + "...")

        # Save to JSON file
        with open('blog_post.json', 'w', encoding='utf-8') as f:
            json.dump(post_data, f, indent=2, ensure_ascii=False)
        print("\n" + "-" * 60)
        print("Full post data saved to 'blog_post.json'")
    else:
        print("Failed to scrape the blog post")


if __name__ == "__main__":
    main()
