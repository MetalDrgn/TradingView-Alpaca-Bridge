from flask import Flask, request, Response
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce
from settings import options
import os, logging, re, json, time

# from alpaca.trading.models import Position

# Create a logger
logger = logging.getLogger("AlpacaLogger")

# Set the log level to include all messages
logger.setLevel(logging.DEBUG)

# Create a file handler
handler = logging.FileHandler("AlpacaLogger.log")

# Create a formatter and add it to the handler
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)


def getKeys(account):
    load_dotenv(override=True)
    # Get the API keys from the environment variables. These are for Paper keys. Below are keys for real trading in Alpaca
    if account == "paperTrading":
        # Paper trading
        paperTrading = {
            "api_key": os.environ.get("Alpaca_API_KEY"),
            "secret_key": os.environ.get("Alpaca_SECRET_KEY"),
            "paper": True,
        }
        account = paperTrading
    elif account == "realTrading":
        # Real money trading
        realTrading = {
            "api_key": os.environ.get("Alpaca_API_KEY-real"),
            "secret_key": os.environ.get("Alpaca_SECRET-real"),
            "paper": False,
        }
        account = realTrading
    else:
        raise NameError(
            "Verify account type (realTrading/paperTrading) is correct in settings(using:)"
        )
    return account


# Get file path
def filePath():
    return os.path.dirname(__file__)


# Pointer for the type you want to use (real/paper).
account = getKeys(options["using"])


# Load settings
def loadSettings(paper, real, using):
    settings = paper
    if using != "paperTrading":
        for i in real.keys():
            try:
                settings[i]
            except:
                err = f"realTrading/paperTrading setting name mismatch: {i}. Please fix the spelling in realTrading"
                raise Exception(err)
        settings.update(options[options["using"]])
    return settings


settings = loadSettings(options["paperTrading"], options["realTrading"], options["using"])

app = Flask(__name__)

# data examples from pine script strategy alerts:
# Compatible with 'Machine Learning: Lorentzian Classification' indicator alerts
# LDC Kernel Bullish ▲ | CLSK@4.015 | (1)...
# Also compatible with custom stratedy alerts (ex. strategy.entry, strategy.close_all, etc.)
# order sell | MSFT@337.57 | Directional Movement Index...


def acctInfo():
    temp = TradingClient(**account).get_account()
    print(f'***account: {"PAPER" if account["paper"] else "REAL MONEY"}')
    print(f"status: {temp.status}")
    print(f"account blocked: {temp.account_blocked}")
    print(f"trade_suspended_by_user: {temp.trade_suspended_by_user}")
    print(f"trading_blocked: {temp.trading_blocked}")
    print(f"transfers_blocked: {temp.transfers_blocked}")
    print(f"equity: {temp.equity}")
    print(f"currency: {temp.currency}")
    print(f"cash: {temp.cash}")
    print(f"buying_power: {temp.buying_power}")
    print(f"daytrading_buying_power: {temp.daytrading_buying_power}")
    print(f"shorting_enabled: {temp.shorting_enabled}")
    print(f"crypto_status: {temp.crypto_status}")
    print("-------------------------------------------------")


@app.route("/", methods=["POST"])
def respond():
    req_data = str(request.data)
    logger.info(f"Recieved request with data: {req_data}")
    trader = AutomatedTrader(**account, req=req_data)

    return Response(status=200)


class AutomatedTrader:
    """Trader client and functions for buying, selling, and validating of
    orders.'req' is the request that needs to be processed.
    """

    def __init__(self, api_key, secret_key, paper=True, req="", newOptions={}):
        self.options = {
            # Enable/disable shorting. Not fully implemented yet.
            # ***Alert(s) needs to say 'short' and you have to close any long positions first.
            "short": False,
            # Hard set at the moment to 20% of the cash balance. Useful in paper testing if you have around 5 stock alerts you want to analyse.
            # Be careful, if more than one order is going through at the the same time, it may spend over the total cash available and go into margins. Mainly a problem in real money trading.
            # Behaves differently when testMode is enabled.
            "buyPerc": 0.2,
            # Balance is set in the function setBalance().
            "balance": 0,
            # Not used
            "buyBal": 0,
            # Gets open potisions to verify ordering. Multiple buys before selling not implemented yet.
            "positions": [],
            # Retrieves open orders is there are any for the symbol requested.
            "orders": [],
            # Gets all the open orders.
            "allOrders": [],
            # Testmode sets the balance to a predetermined amount set in createOrder.
            # Used to not factor in remaining balance * buyPerc after positions are opened.
            "testMode": True,
            # enabled will allow submission of orders.
            "enabled": True,
            # Setting to True will impose a predefined limit for trades
            "limit": True,
            # How much to limit the buy/sell price. Order not filled before sell will be canceled. Change to buyPerc setting once stock price >limitThreshold.
            "limitamt": 0.04,
            # Limit threshold $ amount to change to % based limit
            "limitThreshold": 100,
            # limit percent for everything above a certain amount which is predefined for now below.
            "limitPerc": 0.0005,
            # Maxtime in seconds before canceling an order
            "maxTime": 10,
        }
        # Use settings if they were imported successfully. More of a debug test since it fails if it's not there and it should be there.
        if settings:
            self.options = settings
        # Count the items in options and if newOptions changes this raise an exception.
        optCnt = len(self.options)
        self.options.update(newOptions)
        if len(self.options) != optCnt:
            raise Exception(
                "Extra options found. Verify newOption keys match option keys"
            )
        # Load keys, request data, and create trading client instance.
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self.client = self.createClient()
        self.req = req
        # Check for configuration conflict that could cause unintended buying.
        if self.options["enabled"] and self.options["testMode"] and not self.paper:
            err = "testMode and real money keys being used, exiting. Switch one or the other."
            logger.error(err)
            raise Exception(err)
        # Verify 'enabled' option is True. Used primarily for unittesting.
        elif self.options["enabled"]:
            self.setData()
            self.setOrders()
            self.setPosition()
            self.setBalance()
            self.createOrder()

    def createClient(self):
        # Creates the trading client based on real or paper account.
        return TradingClient(self.api_key, self.secret_key, paper=self.paper)

    def setData(self):
        # requests parsed for either Machine Learning: Lorentzian
        # Classification or custom alerts (noted in documentation how to setup).
        if self.req[:3] == "LDC":
            extractedData = re.search(
                # regex
                r"(bear|bull|open|close).+?(long|short)?.+[|] (.+)[@]\[*([0-9.]+)\]* [|]",
                self.req,
                flags=re.IGNORECASE,
            )
        else:
            extractedData = re.search(
                r"order (buy|sell) [|] (.+)[@]\[*([0-9.]+)\]* [|]",
                self.req,
                flags=re.IGNORECASE,
            )
        if extractedData == None:
            logger.error(f"Failed to extract incoming request data: {self.req}")
            raise Exception(f"Failed to extract incoming request data: {self.req}")
            # return Response(status=500)
        elif len(extractedData.groups()) == 3:
            self.data = {
                "action": extractedData.group(1),
                "position": None,
                "stock": extractedData.group(2),
                "price": float(extractedData.group(3)),
            }
        elif len(extractedData.groups()) == 4:
            self.data = {
                "action": extractedData.group(1),
                "position": extractedData.group(2),
                "stock": extractedData.group(3),
                "price": float(extractedData.group(4)),
            }
        else:
            err = f"invalid webhook received: {self.req}"
            logger.error(err)
            print(err)

    def setOrders(self):
        # get open orders
        self.options["allOrders"] = self.client.get_orders()
        for x in self.options["allOrders"]:
            print(x.symbol, x.qty)
        stock = GetOrdersRequest(symbols=[self.data["stock"]])
        # self.options['stockOrders'] = self.client.get_orders(stock)
        self.options["orders"] = self.client.get_orders(stock)

    def setPosition(self):
        # get stock positions
        try:
            self.options["positions"] = self.client.get_open_position(
                self.data["stock"]
            )
        except Exception as e:
            # logger.warning(e)
            self.options["positions"] = None

    def setBalance(self):
        # set balance at beginning and after each transaction
        cash = float(self.client.get_account().cash)
        # nMBP = float(self.client.get_account().non_marginable_buying_power)
        acctBal = cash
        # acctBal = cash - (cash-nMBP)*2
        # acctBal = float(self.client.get_account().cash)
        self.options["balance"] = acctBal

    def createOrder(self):
        # Setting papameters for market order
        # Clear uncompleted open orders. Shouldn't be any unless trading is unavailable...
        if len(self.options["orders"]) > 0:
            self.cancelOrderById()

        if self.options["testMode"]:
            # Testing with preset variables
            self.options["balance"] = 100000
        # Check for negative balance
        elif self.options["balance"] < 0:
            logger.warning(f'Negative balance: {self.options["balance"]}')
            self.options["balance"] = 0

        # shares to buy in whole numbers
        amount = int(
            self.options["balance"] * self.options["buyPerc"] / self.data["price"]
        )

        # get position quantity
        posQty = (
            float(self.options["positions"].qty)
            if self.options["positions"] != None
            else 0
        )

        # Setup for buy/sell/open/close/bear/bull/short/long
        if self.data["action"] == "Open" and self.data["position"] == "Short":
            side = OrderSide.SELL
            # Close if shorting not enabled. Need to adjust for positive and negative positions. Done?
            if posQty < 0:
                logger.debug(f'Already shorted for: {self.data["stock"]}')
                amount = 0
                return
            if not self.options["short"] and posQty == 0:
                logger.info(
                    f'Shorting not enabled for: {self.data["stock"]}, {self.data["action"]}, {self.data["position"]}'
                )
                amount = 0
            elif not self.options["short"] and posQty > 0:
                logger.info(
                    f'Selling all positions. Shorting not enabled for: {self.data["stock"]}, {self.data["action"]}, {self.data["position"]}'
                )
                amount = posQty
            elif self.options["short"] and posQty > 0:
                # Can't short with long positions so need to figure out how to sell to 0 then short and vice versa.
                amount = posQty
            elif self.options["short"] and posQty == 0:
                pass
            elif self.options["short"] and posQty < 0:
                logger.info(
                    f'Already shorted for: {self.data["stock"]}, {self.data["action"]}, {self.data["position"]}'
                )
                amount = 0
                return
        elif self.data["action"] == "Close" and self.data["position"] == "Short":
            side = OrderSide.BUY
            # Close positions for symbol
            if posQty > 0:
                logger.debug(
                    f'Short not needed. Already long for: {self.data["stock"]}'
                )
                amount = 0
                return
            elif posQty == 0:
                pass
            elif posQty < 0:
                amount = abs(posQty)
        elif (
            self.data["action"] == "Bull"
            or self.data["action"] == "buy"
            or self.data["action"] == "Open"
        ) and (self.data["position"] == "Long" or self.data["position"] == None):
            side = OrderSide.BUY
            if self.options["positions"] != None:
                amount = 0

        elif (
            self.data["action"] == "Bear"
            or self.data["action"] == "sell"
            or self.data["action"] == "Close"
        ) and (self.data["position"] == "Long" or self.data["position"] == None):
            side = OrderSide.SELL
            # Close positions for symbol. Setting to 0 so it won't run if there's already a position.
            amount = 0
            if self.options["positions"] != None and posQty > 0:
                amount += posQty
            # need to add short depending if shorting is enabled. Not needed?
            # if not self.options['short']:
            #   logger.info(f'Shorting not enabled for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}')
            #   return
        else:
            logger.error(
                f'Unhandled Order: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}'
            )

        # return if 0 shares are to be bought. Basically not enough left over for buying 1 share or more
        if amount == 0:
            logger.info(
                f'0 Orders requested: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}'
            )
            return
        # return if less then 0 shares are to be bought. Shouldn't happen right now
        elif amount < 0:
            logger.info(
                f'<0 Orders requested: {self.data["stock"]}, {self.data["action"]}, {self.data["price"]}, amount: {amount}'
            )
            return
        self.orderType(amount, side, self.options["limit"])
        self.submitOrder()

    def orderType(self, amount, side, limit):
        # Setup buy/sell order
        # Market order if "limit" setting set to False
        if not limit:
            order_data = MarketOrderRequest(
                symbol=self.data["stock"],  # "MSFT"
                qty=amount,  # 100
                side=side,
                time_in_force=TimeInForce.GTC,
            )
        # Limit order if "limit" setting set to True
        elif limit:
            # Predefined price to override limitamt with limitPerc*price
            if self.data["price"] > self.options["limitThreshold"]:
                self.options["limitamt"] = (
                    self.data["price"] * self.options["limitPerc"]
                )
            order_data = LimitOrderRequest(
                symbol=self.data["stock"],  # "MSFT"
                limit_price=round(
                    self.data["price"]
                    + (
                        self.options["limitamt"]
                        if side == OrderSide.BUY
                        else -self.options["limitamt"]
                    ),
                    2,
                ),
                qty=amount,  # 100
                side=side,
                time_in_force=TimeInForce.GTC,
            )
        self.order_data = order_data

    def submitOrder(self):
        try:
            self.order_data
        except AttributeError:
            return
        # escape and don't actually submit order if not enabled. For debugging/testing purposes.
        if not self.options["enabled"]:
            logger.debug(
                f'Not enabled, order not placed for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
            )
            return
        # Submit order
        self.order = self.client.submit_order(self.order_data)
        self.verifyOrder(self.order)
        # Need to add while look that checks if the order finished. if limit sell failed, change to market order or something like that. For buy just cancel or maybe open limit then cancel?

    def verifyOrder(self, order=None, timeout=False):
        # Verify order exited in 1 of 3 ways (cancel, fail, fill).
        # TODO: Need to add async stream method for checking for order completion.
        maxTime = self.options["maxTime"]
        totalMaxTime = self.options["totalMaxTime"]
        orderSideBuy = str(order.side) == "OrderSide.BUY"
        orderSideSell = str(order.side) == "OrderSide.SELL"
        now = time.time()
        id = order.client_order_id
        # While loop that checks the order status every 1 second.
        while (
            order.filled_at is None
            and order.failed_at is None
            and order.canceled_at is None
        ):
            if time.time() - now > maxTime and not timeout:
                logger.debug(
                    f'Order exeeded max time ({maxTime} seconds) for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                )
                if (self.options["buyTimeout"] == "Cancel" and orderSideBuy) or (
                    self.options["sellTimeout"] == "Cancel" and orderSideSell
                ):
                    self.cancelOrderById(order.id.hex)
                    # Refreshes status of order before verifying to speed up the process.
                    order = self.client.get_order_by_client_id(id)
                    timeout = True
                    if not self.verifyOrder(order, True):
                        err = "cancel order failed"
                        print(err)
                        logger.debug(
                            f'Order cancel failed for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                        )
                elif (self.options["buyTimeout"] == "Market" and orderSideBuy) or (
                    self.options["sellTimeout"] == "Market" and orderSideSell
                ):
                    self.cancelOrderById(order.id.hex)
                    # Refreshes status of order before verifying to speed up the process.
                    order = self.client.get_order_by_client_id(id)
                    timeout = True
                    if self.verifyOrder(order, True):
                        print("verified canceled order")
                        amount = float(order.qty) - float(order.filled_qty)
                        self.orderType(amount, order.side, False)
                        order = self.client.submit_order(self.order_data)
                        if self.verifyOrder(order, True):
                            logger.debug(
                                f'Timeout market order succeeded for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                            )
                        else:
                            err = "market order failed"
                            print(err)
                            logger.debug(
                                f'Timeout market order failed for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                            )
                    else:
                        err = "cancel order failed"
                        print(err)
                        logger.debug(
                            f'Order cancel failed for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                        )
                        raise Exception(err)
                else:
                    err = "buy or sell timeout setting not found. Check the spelling in the settings and relaunch the server"
                    logger.error(err)
                    raise Exception(err)
            elif time.time() - now > totalMaxTime:
                self.cancelOrderById(order.id.hex)
                # failsafe to exit loop
                logger.warning(
                    f'Order exeeded totalMaxTime of {totalMaxTime} seconds for: action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
                )
                # break
                return True
            time.sleep(1)
            order = self.client.get_order_by_client_id(id)

        if order.canceled_at is not None:
            logger.debug(
                f'Order canceled for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
            )
            return True
        elif order.failed_at is not None:
            logger.warning(
                f'Order failed for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
            )
            return False
        elif order.filled_at is not None:
            logger.info(
                f'Order filled for: {self.data["stock"]}, action: {self.data["action"]}, price: {self.data["price"]}, quantity: {self.order_data.qty}'
            )
            return True

    def cancelOrderById(self, id=None):
        if not self.options["enabled"]:
            value = f'Trading not enable, order not canceled for: {self.data["stock"]}, {self.data["action"]}, {self.data["price"]}'
            logger.debug(value)
            return value
        elif id != None:
            self.client.cancel_order_by_id(id)
            # self.verifyOrder()
            return
        for x in self.options["orders"]:
            self.client.cancel_order_by_id(x.id.hex)
            logger.info(
                f'Canceled order for: {self.data["stock"]}, {self.data["action"]}, {self.data["position"]}, id: {x.id.hex}'
            )

    def cancelAll(self):
        # Not used
        self.canxStatus = self.client.cancel_orders()


if __name__ == "__main__":
    # Display general account info.
    acctInfo()
    # Start the app
    app.run(port=5000, debug=False, threaded=True)