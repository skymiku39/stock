"""Route candidate symbols; issuer identity is still verified against raw sources."""
import re


def sec_symbol(ticker):
    symbol, _, market = ticker.partition(' ')
    if market and market not in {'US', 'UW'}:
        return None
    return symbol if re.fullmatch(r'[A-Z][A-Z0-9.-]*', symbol) else None


def supported_ticker(ticker):
    return bool(re.fullmatch(r'[A-Za-z0-9.^-]+(?: (?:US|UW|JP|JT|KS|GY))?', ticker))


def unsupported_reason(ticker):
    _, _, market = ticker.rpartition(' ')
    return (f'official source adapter not configured for market {market}'
            if market and ' ' in ticker else 'unrecognized ticker identity')
