import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import urlopen


def request(url):
    try:
        with urlopen(url, timeout=10) as response:
            return response.status
    except HTTPError as error:
        return error.code


def main():
    parser = argparse.ArgumentParser(description="Generate traffic for Grafana dashboards.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--failures", type=int, default=20)
    args = parser.parse_args()

    urls = [f"{args.base_url}/api/organize"] * args.requests
    urls.extend([f"{args.base_url}/api/failure"] * args.failures)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        statuses = list(executor.map(request, urls))

    successful = sum(status < 400 for status in statuses)
    failed = len(statuses) - successful
    print(f"Completed: {len(statuses)}, successful: {successful}, failed: {failed}")
    print("Keep Grafana open for at least one scrape interval to inspect the spike.")
    time.sleep(1)


if __name__ == "__main__":
    main()