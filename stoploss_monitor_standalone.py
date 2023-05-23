import os
from os.path import exists
import PySimpleGUI as sg
import pandas as pd

import config
import time
from datetime import datetime, timedelta
import httpx
import sys
import json

from tda import orders, utils, auth
from tda.orders.options import bull_put_vertical_open, bull_put_vertical_close, option_buy_to_close_stop
from tda.orders.generic import OrderBuilder
from tda.auth import easy_client
from tda.client import Client
from tda.utils import Utils
from tda.streaming import StreamClient

# For threading
import threading
from threading import Event

# For regular expression functionality (used to extract strike from option symbol)
import re

# For discord notifications
from discordwebhook import Discord      # for sending messages to discord
# For discord notifications
from discordwebhook import Discord

# GLobal Variables
spx_value = 0

# Important: Change the following link to your own discord webhook
discord = Discord(url=config.DISCORD_HOOK)

 # 0: No notifications will be sent to discord, 1: will only send important notifications, 2: will send notifications for all actions
discord_notification_level = 0                  

########################################################### 
#           Returns TD Client 
########################################################### 
# Setup TD Client. It will use the token and if token is expired the login window will pop up.
# Requirements: a config file with user credentials
def create_td_client() :
    try:
        client = easy_client(api_key=config.API_KEY, redirect_uri=config.REDIRECT_URI, token_path=config.TOKEN_PATH)
    except FileNotFoundError:
        from selenium import webdriver
        with webdriver.Chrome() as driver:
            client = client_from_login_flow(driver, api_key=config.API_KEY, redirect_uri=config.REDIRECT_URI, token_path=config.TOKEN_PATH)  

    return client


###########################################################
#           Checks status of TD Token
###########################################################
def check_auth_token():
    # Read TDA token to note token expiration
    with open(config.TOKEN_PATH) as file:
        data = json.load(file)

    # print(json.dumps(data, indent=4))

    token_created = pd.to_datetime(data['creation_timestamp'], unit='s')
    token_expires = token_created + timedelta(days=90)
    print(" Authentication Token Created: ",  str(token_created), " Will Expire: ", str(token_expires))

    # add warning when nearing expiration
    if (token_expires < datetime.now() - timedelta(days=7)):
        print("  --**-- Authorization token expiring soon. Run token_renew.py to renew.")
    
    return


###########################################################
#   Converts SPX price to 5 cent units
###########################################################
# SPX opotions are priced at 5 cent incrememts, therefore we need to convert
# prices in nickles. The following function converts a price to the nearest nickle.
#
# Needs
#     org_price: original price
# Returns
#     new_price: price in 5C units
#
def nicklefy(org_price):
    new_price = org_price * 100                    # bring up to whole
    new_price = round(new_price/5, 0) * 5 / 100    # convert to a 5 cent mark
    new_price = round(new_price, 2)

    return new_price

########################################################### 
#           Finds missing Stops
########################################################### 
# Needs:
#   open_shorts: list of option symbols for open positions
#   working_stops: list of option symbols that have WORKING STOP orders in place
# Returns:
#   num_missing: number of open positions that have missing Stops
#   missing_symbols: a list of option symbols that have missing stops
#
def find_missing_stops(qty_open_short_df, qty_work_stop_df, open_shorts, quantity_open_shorts, avg_price_open_shorts, working_stops, quantity_working_stops) :
    num_missing = 0
    missing_symbols = []
    missing_quantity = []
    missing_avg_price = []

    num_shorts = len(open_shorts)
    num_stops = len(working_stops)
    print("Num Shorts = ", num_shorts)
    print("Num Stops = ", num_stops)

    for i in range(0, num_shorts) :
        
        if open_shorts[i] in working_stops :
            print("Stop exists for option: ", open_shorts[i])

            if open_shorts[i] in qty_open_short_df['symbol'].values :   

                # Make sure that the quantity in the working STOP order matches the quantity in the open short positions.
                # If not, cancel existing SHORT order and resubmit it with the correct quantity.
                qty_open_short = int(qty_open_short_df.loc[qty_open_short_df['symbol'] == open_shorts[i], 'quantity'].iloc[0])
                qty_work_stop = int(qty_work_stop_df.loc[qty_work_stop_df['symbol'] == open_shorts[i], 'quantity'].iloc[0])
                avg_price = float(qty_open_short_df.loc[qty_open_short_df['symbol'] == open_shorts[i], 'price'].iloc[0])

                if qty_open_short != qty_work_stop:
                    print("Missmatch in quantity between working STOP order and Open Short position for: " + str(open_shorts[i]))

                    # Send notification to discord
                    if discord_notification_level != 0:
                        discord_message = "Missmatch in quantity between working STOP order and Open Short position for: " + str(open_shorts[i])
                        discord.post(content=discord_message)

                    num_missing = num_missing + 1
                    missing_symbols.append(open_shorts[i]) 
                    missing_quantity.append(qty_open_short)
                    missing_avg_price.append(avg_price)
            else:
                print("No order ID found for: ", open_shorts[i], ", will skip this position . . .")

        else : 
            print("Stop is missing for option: ", open_shorts[i])

            # Send notification to discord
            if discord_notification_level != 0:
                discord_message = "Stop is missing for option: " + str(open_shorts[i])
                discord.post(content=discord_message)

            num_missing = num_missing + 1
            missing_symbols.append(open_shorts[i]) 
            missing_quantity.append(quantity_open_shorts[i])
            missing_avg_price.append(avg_price_open_shorts[i])

    return num_missing, missing_symbols, missing_quantity, missing_avg_price


########################################################### 
#       In-The-Money Calculator
########################################################### 
# It detects any short position within itm_offset distance from the SPX value
#
def find_itm_short_positions(itm_offset, open_shorts, quantity_open_shorts, working_stops):
    distance_allowed = float(itm_offset)

    num_itm = 0
    itm_symbols = []
    itm_quantity = []
    itm_stop_order_id = []

    num_shorts = len(open_shorts)
    num_stops = len(working_stops)
    for i in range(0, num_shorts) :
        numbers = re.findall(r"\d+", open_shorts[i])
        if numbers:
            strike_short = float(numbers[1])
        else:
            print("No strike found.")
            strike_short = 0

        if abs(spx_value - strike_short) <= distance_allowed:
            print("ITM protection activated for: ", open_shorts[i])
            num_itm = num_itm + 1
            itm_symbols.append(open_shorts[i]) 
            itm_quantity.append(quantity_open_shorts[i])
            stop_order_id = find_stop_order_id(open_shorts[i], working_stops)
            itm_stop_order_id.append(stop_order_id)

    return  num_itm, itm_symbols, itm_quantity, itm_stop_order_id 

# Finds STOP trigger based on the average FILL price of a particular order. We may have positions at the same
# SHORT strike but at different FILL prices.
def find_num_stops_required(missed_symbol, df_orders):
    grouped = df_orders.groupby('symbol')
    num_orders = grouped.get_group(missed_symbol)
    return len(num_orders)

def find_stop_trigger(multiplier, missed_symbol, df_orders):
    grouped = df_orders.groupby('symbol')
    stop_trigger_df = grouped.get_group(missed_symbol).copy()
    stop_trigger_df['trigger'] = multiplier * stop_trigger_df['price']  # add trigger column to the dataframe
    return stop_trigger_df

def calc_symbol_quantity(df_order_tracker):
    # Calculate the sum of columns based on the 'symbol' column
    temp_df = df_order_tracker.groupby('symbol').sum().reset_index()
    quantity_df = temp_df.copy()
    return quantity_df

########################################################### 
#   Retrieves open positions using a call to TD Client
########################################################### 
#   fields = client.Account.Fields('orders')            # Returns all orders, including filled, cancelled, working, etc. If we use status=FILLED, it will return same infor as with positions call
#   fields = client.Account.Fields('positions')         # Returns orders that were FILLED
#
def get_open_positions(client):
    fields = client.Account.Fields('positions')
    try:
        response = client.get_account(account_id=config.ACCOUNT_ID_REGULAR, fields=fields)
    except (httpx.ConnectError, httpx.TimeoutException):
        response = None

    return response


########################################################### 
#   Retrieves orders book using a call to TD Client
########################################################### 
#   fields = client.Account.Fields('orders')            # Returns all orders, including filled, cancelled, working, etc. If we use status=FILLED, it will return same infor as with positions call
#   fields = client.Account.Fields('positions')         # Returns orders that were FILLED
#
def get_orders_book(client):
    fields = client.Account.Fields('orders')
    try:
        response = client.get_account(account_id=config.ACCOUNT_ID_REGULAR, fields=fields)
    except (httpx.ConnectError, httpx.TimeoutException):
        response = None

    return response


########################################################### 
# Returns a dataframe of all orders that match the filter
########################################################### 
# Needs:
#   orders_list: a list of type r['securitiesAccount']['orderStrategies'] extracted from JSON respinse from TD API
#   filter: the status filter to apply on the list, e.g. 'WORKING', 'FILLED', 'EXPIRED, etc.
# Returns:
#   df_all: a dataframe with keys "order_id", "leg_id", "datetime", "underlying", "buy_sell", "symbol", "quantity", "status"
#
def filter_orders_working(orders_list, filter):
    print("Filtering orders of type: ", filter)

    # Convert JSON orders_list to dictionary with the order_id, leg_id, datetime, underlying, buy_sell, symbol, quantity, status
    order_dict = {}
    keys = ["order_id", "leg_id", "datetime", "underlying", "buy_sell", "symbol", "quantity", "status"]
    order_index = 1
    leg_index = 1
    df_all = pd.DataFrame(order_dict)
    data = []  
    
    # print("=====> num orders = ", len(orders_list))
    num_orders_in_filter = 0
    
    # FUTURE WORK: get this to work with OCO and 1st trigger , etc. orders. For now we will skip those
    for order_strat in orders_list:  
        # print("Processing order: ", order_index)  

        if "childOrderStrategies" in order_strat :
            print("Advanced order is being skipped for now")

        else :
            status = order_strat['status']

            # Apply status filter
            if status == filter :
                num_orders_in_filter = num_orders_in_filter + 1
                order_id = order_strat['orderId']
                time_value = order_strat['enteredTime']
                quantity = order_strat["quantity"]
                
                # For multi-leg strats, get values of each leg from orderLegCollection
                for legs in order_strat['orderLegCollection']:
                    # The variable names used here will become columns of the dataframe
                    leg_id = legs['legId']
                    #effect_value = legs['positionEffect']
                    buy_sell = legs['instruction']
                    inst = legs['instrument']
                    opt_symbol = inst['symbol']
                    usymbol_value = inst['underlyingSymbol']
                    
                    #print("Leg: ", str(leg_index) + " effect_value: ", buy_sell, " inst: ", inst, " opt_symbol: ", opt_symbol)
                    # Add to dictionar
                    # keys = ["order_id", "leg_id", "datetime", "underlying", "buy_sell", "symbol", "quantity", "status"]
                    order_dict[keys[0]] = order_id
                    order_dict[keys[1]] = leg_id
                    order_dict[keys[2]] = time_value
                    order_dict[keys[3]] = usymbol_value
                    order_dict[keys[4]] = buy_sell
                    order_dict[keys[5]] = opt_symbol
                    order_dict[keys[6]] = quantity
                    order_dict[keys[7]] = status

                    # # Print dict
                    # print("Printing Dictionary . . .")
                    # print(order_dict)

                    new_df = pd.DataFrame(order_dict, index=[0])
                    data.append(new_df)

                    leg_index = leg_index + 1

            order_index = order_index + 1

    
    if num_orders_in_filter > 0 :
        # print("Number of orders that matched the filter = ", num_orders_in_filter)

        df_all = pd.concat(data, ignore_index=True)

        # # Print Dataframe
        # print("")
        # print("Dataframe of orders:")
        # print(df_all)
        # print("")

        # # save the dataframe to a CSV file
        # # The index=False parameter is used to exclude the index column from the output CSV file.
        # df_all.to_csv('logs/sample.csv', index=False)

    else :
        print("No orders were found that matched the status filter within the date range!")

    return df_all


def get_leg_price(legID, orderActivityCollection):
    # print(json.dumps(orderActivityCollection, indent=4))
    
    orderDict = orderActivityCollection[0]
    orderExecList = orderDict['executionLegs']

    for legs in orderExecList:
        leg_id = legs['legId']
        price = legs['price']

        if leg_id == legID:
            return price


# Buld a dataframe of Filled STO orders
def filter_orders_filled(orders_list, filter):
    print("Filtering orders of type: ", filter)

    # Convert JSON orders_list to dictionary with the order_id, leg_id, datetime, underlying, buy_sell, symbol, quantity, status
    order_dict = {}
    keys = ["order_id", "leg_id", "datetime", "underlying", "buy_sell", "symbol", "quantity", "status", "price"]
    order_index = 1
    leg_index = 1
    df_all = pd.DataFrame(order_dict)
    data = []  
    
    # print("=====> num orders = ", len(orders_list))
    num_orders_in_filter = 0
    
    # FUTURE WORK: get this to work with OCO and 1st trigger , etc. orders. For now we will skip those
    for order_strat in orders_list:  
        # print("Processing order: ", order_index)  

        if "childOrderStrategies" in order_strat :
            print("Advanced order is being skipped for now")
        else :
            # Apply status filter
            status = order_strat['status']
            
            if status == filter:
                #print(json.dumps(order_strat, indent=4))

                num_orders_in_filter = num_orders_in_filter + 1
                order_id = order_strat['orderId']
                time_value = order_strat['enteredTime']
                quantity = order_strat["quantity"]
                
                # For multi-leg strats, get values of each leg from orderLegCollection
                for legs in order_strat['orderLegCollection']:
                    # The variable names used here will become columns of the dataframe
                    leg_id = legs['legId']
                    buy_sell = legs['instruction']
                    inst = legs['instrument']
                    opt_symbol = inst['symbol']

                    if (legs['orderLegType'] == 'OPTION'):
                        usymbol_value = inst['underlyingSymbol']
                    else:
                        num_orders_in_filter = num_orders_in_filter - 1   # we are only conv=cerned about OPTIONs for now
                        break

                    
                    #print("Leg: ", str(leg_index) + " effect_value: ", buy_sell, " inst: ", inst, " opt_symbol: ", opt_symbol)
                    # Add to dictionar
                    # keys = ["order_id", "leg_id", "datetime", "underlying", "buy_sell", "symbol", "quantity", "status", "price"]
                    if buy_sell == 'SELL_TO_OPEN':
                        order_dict[keys[0]] = order_id
                        order_dict[keys[1]] = leg_id
                        order_dict[keys[2]] = time_value
                        order_dict[keys[3]] = usymbol_value
                        order_dict[keys[4]] = buy_sell
                        order_dict[keys[5]] = opt_symbol
                        order_dict[keys[6]] = quantity
                        order_dict[keys[7]] = status
                                        
                        # Get individual leg price
                        leg_price = get_leg_price(leg_id, order_strat['orderActivityCollection'])
                        order_dict[keys[8]] = leg_price

                        # # Print dict
                        # print("Printing Dictionary . . .")
                        # print(order_dict)

                        # Add new row to dataframe
                        new_df = pd.DataFrame(order_dict, index=[0])
                        data.append(new_df)

                        leg_index = leg_index + 1

            order_index = order_index + 1

    
    if num_orders_in_filter > 0 :
        # print("Number of orders that matched the filter = ", num_orders_in_filter)

        df_all = pd.concat(data, ignore_index=True)

        # # Print Dataframe
        # print("")
        # print("Dataframe of orders:")
        # print(df_all)
        # print("")

        # # save the dataframe to a CSV file
        # # The index=False parameter is used to exclude the index column from the output CSV file.
        # df_all.to_csv('logs/sample.csv', index=False)

    else :
        print("No orders were found that matched the status filter within the date range!")

    return df_all


########################################################### 
#   Returns OPTION instruments dataframe
########################################################### 
# Needs:
#   pos_dict: positions dictionary retrived from response of TD Client r['securitiesAccount']['positions'] 
# Returns:
#   df_all: a dataframe with keys "symbol", "putCall", "shortQuantity", "averagePrice"
#  
def create_option_position_df(pos_dict) :
    # Convert JSON response to dictionary with the order_id, leg_id, datetime, underlying, buy_sell, symbol, quantity, status
    order_dict = {}
    keys = ["symbol", "putCall", "shortQuantity", "averagePrice"]

    df = pd.DataFrame(order_dict)
    data = []

    pos_indx = 0
    for pos in pos_dict:
        if pos['instrument']['assetType'] == "OPTION" and pos['shortQuantity'] > 0:
            symbol = pos['instrument']['symbol']       
            putCall = pos['instrument']['putCall']
            shortQuantity = pos['shortQuantity']
            averagePrice = pos['averagePrice']

            order_dict[keys[0]] = symbol
            order_dict[keys[1]] = putCall
            order_dict[keys[2]] = shortQuantity
            order_dict[keys[3]] = averagePrice

            new_df = pd.DataFrame(order_dict, index=[0])
            #new_df.insert(0, 'TimeStamp', pd.to_datetime('now').replace(microsecond=0))   # add timestamp
            data.append(new_df)

            pos_indx = pos_indx + 1
        
    df_all = pd.concat(data, ignore_index=True)


    # For debugging or record keeping, we can save dataframe to a CSV file
    #df_all.to_csv('logs/option_positions.csv', index=False)

    return df_all


########################################################### 
#   Returns FIXED_INCOME instruments dataframe
########################################################### 
# Needs:
#   pos_dict: positions dictionary retrived from response of TD Client r['securitiesAccount']['positions'] 
# Returns:
#   df_all: a dataframe with keys "cusip", "description", "maturityDate", "quantity"
#  
def create_fixed_income_df(pos_dict) :
    # Convert JSON response to dictionary with the order_id, leg_id, datetime, underlying, buy_sell, symbol, quantity, status
    order_dict = {}
    keys = ["cusip", "description", "maturityDate", "quantity"]

    df = pd.DataFrame(order_dict)
    data = []

    pos_indx = 0
    for pos in pos_dict:
        if pos['instrument']['assetType'] == "FIXED_INCOME" :
            cusip = pos['instrument']['cusip']
            descrip = pos['instrument']['description']
            time_value = pos['instrument']['maturityDate']
            quantity = pos['instrument']['factor']

            order_dict[keys[0]] = cusip
            order_dict[keys[1]] = descrip
            order_dict[keys[2]] = time_value
            order_dict[keys[3]] = quantity

            new_df = pd.DataFrame(order_dict, index=[0])
            data.append(new_df)

            pos_indx = pos_indx + 1
        
    df_all = pd.concat(data, ignore_index=True)

    return df_all

########################################################### 
#   Returns EQUITY instruments dataframe
########################################################### 
# Needs:
#   pos_dict: positions dictionary retrived from response of TD Client r['securitiesAccount']['positions'] 
# Returns:
#   df_all: a dataframe with keys "symbol", "shortQuantity", "longQuantity", "averagePrice"
#  
def create_equities_df(pos_dict) :
    # Convert JSON response to dictionary with the order_id, leg_id, datetime, underlying, buy_sell, symbol, quantity, status
    order_dict = {}
    keys = ["symbol", "shortQuantity", "longQuantity", "averagePrice"]

    df = pd.DataFrame(order_dict)
    data = []

    pos_indx = 0
    for pos in pos_dict:
        if pos['instrument']['assetType'] == "EQUITY" :
            symbol = pos['instrument']['symbol']
            shortQuantity = pos['shortQuantity']
            longQuantity = pos['longQuantity']

            averagePrice = pos['averagePrice']

            order_dict[keys[0]] = symbol
            order_dict[keys[1]] = shortQuantity
            order_dict[keys[2]] = longQuantity
            order_dict[keys[3]] = averagePrice

            new_df = pd.DataFrame(order_dict, index=[0])
            data.append(new_df)

            pos_indx = pos_indx + 1
        
    df_all = pd.concat(data, ignore_index=True)

    return df_all


########################################################### 
#   Create a simple layout for the GUI
########################################################### 
# Select a theme
sg.theme('DarkGrey9')

# Each row in the layout represents a column in the GUI
layout = [
    # Text element to display current SPX value
    [sg.Text('SPX: ', font=('Arial Bold', 15), justification='left'), sg.Text('', font=('Arial Bold', 15), justification='center', key='SPXValue')],

    # Simple Text 
    [sg.Text('Please fill out the following fields:')],
    
    # Text and Input box. Text is 15 characters wide & 1 character tall. In the input field the essential thing is the key
    # We will use the key to retrive value from the input form
    # Timer at which to run the monitor at
    [sg.Text('Loop Timer (seconds)', size=(20, 1)), sg.InputText(size=(10, 1), default_text='2.0', key='loop_timer')],

     # Combo box with 2 types of STOP losses to chose from
    [sg.Text('Stop Type', size=(20, 1)), sg.Combo(['Fix', 'Multiplier'], default_value='Fix', size=(10, 1), key='stop_type')],

    # Text and input box
    [sg.Text('Stop Trigger', size=(20, 1)), sg.InputText(size=(10, 1), default_text='2.5', key='stop_trigger')],

    # Text and input box
    [sg.Text('ITM Protection Offset', size=(20, 1)), sg.InputText(size=(10, 1), default_text='1.0', key='itm_protection_offset')],

    # Submit STOP order for missing stops
    # If a user checks a checkbox, it will return True otherwise False
    [sg.Text('Submit Orders for Missing Stops', size=(25,1)), sg.Checkbox('', default=True, key='submitStopOrders'),],
    
    # Buttons
    [sg.Submit('Start'), sg.Button('Stop'), sg.Button('Clear'), sg.Exit()]
    #[sg.Button('Start SL Monitor', button_type=sg.Submit()), sg.Button('Stop SL Monitor'), sg.Button('Clear Form'), sg.Button('Close Form', sg.Exit())]
]


########################################################### 
#       GUI Functions
########################################################### 
# Function to clear entries from GUI form
def clear_input():
    for key in values:
        window[key]('')
    return None

# Function to stop the thread running stop loss monitor function
def stop_thread(event):
    # Stop the task thread first
    print('Stopping thread . . . ')
    event.set()
    return None

########################################################### 
#       Submits STOP orders
########################################################### 
# Needs:
#   symbol: option symbol
#   quantity: quantity
#   trigger: stop trigger price
# Returns:
#   order_id: order ID os the stop order placed
#
def float_to_string(number):
    formatted_string = "{:0.02f}".format(number)
    return formatted_string

def sumbit_stop_orders(client, symbol, quantity, trigger) :
    trigger_float = nicklefy(float(trigger))
    trigger_string = float_to_string(trigger_float)
    print(" Preparing STOP order for = ", symbol, " quantity = ", quantity, " with STOP at = ", trigger)
    stop_order = option_buy_to_close_stop(symbol, quantity, trigger_string)  # In order to avoid rounding issues, use string instead of float for the price
    stop_order.set_duration(orders.common.Duration.GOOD_TILL_CANCEL) 

    # Place the Stop order
    r = client.place_order(config.ACCOUNT_ID_REGULAR, stop_order)  
    
    print("Order status code - ", r.status_code)
    if r.status_code < 400:  # http codes under 400 are success. usually 200 or 201
        order_id = Utils(client, config.ACCOUNT_ID_REGULAR).extract_order_id(r)
        print("Order placed, order ID-", order_id)
    else:
        print("FAILED - placing the order failed.")

        # Send notification to discord
        if discord_notification_level != 0:
            discord_message = "Failed placing the STOP Order"
            discord.post(content=discord_message)

        return

    # This order ID can then be used to monitor or modify the order
    print("Buy to Close order placed, order ID:", order_id)

    # Send notification to discord
    if discord_notification_level != 0:  
        discord_message = "Buy to Close order placed, order ID: " + str(order_id)
        discord.post(content=discord_message)

    return order_id


def replace_stop_with_market_order(client, orig_order_id, symbol, quantity) :
    # Prepare the BTC Market order
    btc_market_order = option_buy_to_close_market(symbol, quantity)
    btc_market_order.set_duration(orders.common.Duration.GOOD_TILL_CANCEL) 

    if orig_order_id == 0:
        # Place BTC Market order
        r = client.place_order(config.ACCOUNT_ID_REGULAR, btc_market_order)  
    else:
        # Replace existing BTC Stop order with BTC Market order
        r = client.replace_order(config.ACCOUNT_ID_REGULAR, itm_order_id, btc_market_order)
    
    print("Order status code - ", r.status_code)
    if r.status_code < 400:  # http codes under 400 are success. usually 200 or 201
        order_id = Utils(client, config.ACCOUNT_ID_REGULAR).extract_order_id(r)
        print("Order placed, order ID-", order_id)
    else:
        print("FAILED - placing the order failed.")

        # Send notification to discord
        if discord_notification_level != 0:
            discord_message = "Failed placing the BTC Market Order"
            discord.post(content=discord_message)

        return

    # This order ID can then be used to monitor or modify the order
    print("Buy to Close Market order placed, order ID:", order_id)

    # Send notification to discord
    if discord_notification_level != 0:  
        discord_message = "Buy to Close order placed, order ID: " + str(order_id)
        discord.post(content=discord_message)

    return order_id

def find_order_id_for_position(symbol, stop_orders_df):
    order_id_list = []

    # If a working stop order exists return its order id, otherwise return 0
    num_orders = stop_orders_df['orderID'].value_counts()[symbol]

    if num_orders > 0:
        for i in range(0, count) :
            order_id_list[i] = stop_orders_df.iloc[i]['order_id']
    else:
        order_id_list = [0]

    return num_orders, order_id_list

########################################################### 
#       Stop orders monitor
########################################################### 
# Needs:
#   stop_type: type of STOP, either 'Fix' or 'Multiplier'
#   stop_trigger: trigger price for the STOP
#   submit_stop_orders: a flag, either 'TRUE' or 'FALSE'
#       If 'TRUE' stop order will be placed for an open
#       short option position that does not have a 
#       corresponding stop order in WORKING status.
#
def stop_monitor(event, loop_timer, stop_type, stop_trigger, submit_stop_orders, itm_offset):
    # Create a TD API client
    client = create_td_client()
    
    while True:
        print("The Watcher is monitoring Short positions: ", datetime.now())

        # If "Stop" button is pressed on the GUI, end Stop Loss Monitor thread
        if event.is_set():
            print("Stopped The Watcher task at: ", datetime.now())
            break

        #----------------------------------------------------------------------------------------
        # Step 1: Get open positions for a given account_ID (we will need to read positions)
        #----------------------------------------------------------------------------------------
        response = get_open_positions(client)
        r = json.load(response)  # Convert to JSON
        num_open_pos = len(r)
        print("Number of open positions = ", num_open_pos)

        # calculate number of positions for various instruments
        num_fixed_income = 0
        num_equities = 0
        num_options_short = 0
        num_other = 0

        pos_indx = 0
        positions_dict = r['securitiesAccount']['positions']           # extract positions list from response
        for current_pos in positions_dict:  
            #print(json.dumps(current_pos, indent=4))
            #print("Processing position: ", pos_indx)  

            inst_type = current_pos['instrument']['assetType']
            if inst_type == "FIXED_INCOME":
                num_fixed_income = num_fixed_income + 1
            elif inst_type == 'EQUITY':
                num_equities = num_equities + 1
            elif inst_type == 'OPTION' and current_pos['shortQuantity'] > 0:
                num_options_short = num_options_short + 1
            else:
                num_other = num_other + 1

            pos_indx = pos_indx + 1

        # print("Number of FIXED INCOME positions = ", num_fixed_income)
        # print("Number of EQUITY positions = ", num_equities)
        print("Number of open SHORT OPTION positions = ", num_options_short)

        # Send notification to discord
        if discord_notification_level == 2:
            discord_message = "Number of open SHORT OPTION positions = " + str(num_options_short)
            discord.post(content=discord_message)

        #----------------------------------------------------------------------------------------
        # Step 2: Get orders book for today (we will need to read orders not positions)
        #----------------------------------------------------------------------------------------
        response = get_orders_book(client)
        r = json.load(response)  # Convert to JSON
        # num_orders = len(r)
        # print("Number of orders = ", num_orders)

        # Extract FILLED orders list
        orders_filled_list = r['securitiesAccount']['orderStrategies']
        filter_order_type = 'FILLED'
        df_filled_orders = filter_orders_filled(orders_filled_list, filter_order_type)
        num_filled_orders = len(df_filled_orders)

        # Print orders Dataframe
        print("")
        print("Filled Orders:")
        print(df_filled_orders)
        print("")

        # Stop Monitor is intended to be used for "Today's" trades.
        # Therefore, if there are no open short positions and no FILLED orders (for today), it will 
        # idle until an order is FILLED
        if ((num_options_short > 0) and (num_filled_orders > 0)):
            df_pos = create_option_position_df(positions_dict)

            # Print positions Dataframe
            print("")
            print("Open Positions:")
            print(df_pos)
            print("")

            # Following dataframe keeps track of order ID and short strikes. This information is used to calculate
            # multiplier loss for scenarios when there are orders for the same strike but different entry price. This
            # may lead to multiple STOP orders because we might have STO 1 lot at price x and the other lot at price y.
            # For the fix STOP this information is not useful because we will always use a fix STOP.
            df_order_tracker = df_filled_orders[['symbol', 'order_id', 'quantity', 'price']]
            # print(df_order_tracker)

            # Calculate total quantity per symbol from all the orders
            df_symbol_qty = df_filled_orders[['symbol', 'quantity', 'price']]
            quantity_pos_df = calc_symbol_quantity(df_symbol_qty)

            # Calculate average price
            for i in range(0, len(quantity_pos_df)) :
                x = quantity_pos_df.iloc[i]['price'] /quantity_pos_df.iloc[i]['quantity'] 
                quantity_pos_df.loc[i, 'price'] = x

            print("")
            print("Quantity in positions dataframe:")
            print(quantity_pos_df)
            print("")  

            # Extract working orders list
            orders_working_list = r['securitiesAccount']['orderStrategies']
            filter_order_type = 'WORKING'
            df_stop = filter_orders_working(orders_working_list, filter_order_type)

            # Print orders Dataframe
            print("")
            print("Working Stops:")
            print(df_stop)
            print("")

            # Calculate total quantity per symbol from all the working stop orders
            df_symbol_qty2 = df_stop[['symbol', 'quantity']]
            quantity_stop_df = calc_symbol_quantity(df_symbol_qty2)
            
            print("")
            print("Quantity in Stops dataframe:")
            print(quantity_stop_df)
            print("")  

            open_shorts = []
            quantity_open_shorts = 0
            avg_price_open_shorts = 0
            if len(df_pos.index > 0):
                open_shorts = df_pos["symbol"].values.tolist()
                quantity_open_shorts = df_pos["shortQuantity"].values.tolist()
                avg_price_open_shorts = df_pos["averagePrice"].values.tolist()
            
            working_stops = []
            quantity_working_stops = 0
            if len(df_stop.index > 0):
                working_stops = df_stop["symbol"].values.tolist()
                quantity_working_stops = df_stop["quantity"].values.tolist()

            #-----------------------------------------
            # Perform In-The-Money Protection check
            #-----------------------------------------
            # If SPX is within distance (defined by the user) to a short position, 
            # replace the existing STOP order with "MARKET" order. If no existing STOP order
            # then submit a new "MARKET" order to close the position
            num_itm_positions, itm_symbols, itm_quantity, stop_order_id = find_itm_short_positions(itm_offset, open_shorts, quantity_open_shorts, working_stops)
            if num_itm_positions > 0:
                print("WARNING: ITM protection activated for the positions:")
                print(itm_symbols)
                
                # Send notification to discord
                if discord_notification_level == 2:
                    discord_message = "WARNING: ITM protection activated for the positions:" + str(missing_symbols)
                    discord.post(content=discord_message)

                for i in range(0, num_itm_positions) :   
                    # Replace BTC STOP ordet with BTC MARKET order     
                    # # Note to self: we could replace existing stops with amrket orders but it is faster to just submit
                    # market orders for the open short quantity. TD does not allow simultaneous buy & sell in the same symbol
                    # so it will practically replace the existing STOP order(s) with the new MARKET order             
                    # itm_order_id = find_order_id_for_position(itm_symbols[i], df_symbol_qty2)
                    itm_order_id = 0
                    replace_stop_with_market_order(client, itm_order_id, itm_symbols[i], int(itm_quantity[i]))


            # Perform missing stops check
            num_missing_stops, missing_symbols, missing_quantity, missing_avg_price = find_missing_stops(quantity_pos_df, quantity_stop_df,
                        open_shorts, quantity_open_shorts, avg_price_open_shorts, working_stops, quantity_working_stops)

            #------------------------------------------------------
            # Step 3: Submit STOP orders for missing positions
            #------------------------------------------------------
            if submit_stop_orders :
                if num_missing_stops > 0 :
                    print("Found missing stops in the following positions:")
                    print(missing_symbols)
                    
                    # Send notification to discord
                    if discord_notification_level == 2:
                        discord_message = "Found missing stops in the following positions:" + str(missing_symbols)
                        discord.post(content=discord_message)

                    # Submit Stop orders for the missing
                    # stop_type, stop_trigger
                    for i in range(0, num_missing_stops) :
                        if stop_type == 'Fix' :
                            trigger = stop_trigger                    
                            sumbit_stop_orders(client, missing_symbols[i], int(missing_quantity[i]), trigger)
                        else :
                            num_stops_required = find_num_stops_required(missing_symbols[i], df_order_tracker)
                            if num_stops_required == 1:
                                # Submit single STOP order at average fill price
                                trigger = float(stop_trigger) * float(missing_avg_price[i])                  
                                sumbit_stop_orders(client, missing_symbols[i], int(missing_quantity[i]), trigger)
                            else:
                                # Submit multiple STOP orders, one for wach order                  
                                trigger_df = find_stop_trigger(stop_trigger, missing_symbols[i], df_order_tracker)

                                for j in range(0, num_stops_required) :
                                    missing_quantity = int(trigger_df.iloc[j]['quantity'])
                                    trigger =  float(trigger_df.iloc[j]['trigger'])                  
                                    sumbit_stop_orders(client, missing_symbols[i], missing_quantity, trigger)

            else :
                print("User selected not to submit missing stops . . . ")

            time.sleep(loop_timer)

        time.sleep(loop_timer)
  
    return


# SPX Value Streamer
def retrieve_SPX_value(client):
    make_trade = True
    startdate = datetime.now()
    # Tomorrow. This will ensure that we get data up to current time
    enddate = datetime.now() + timedelta(days=1)

    option_chain = {}
    contract_type_call = client.Options.ContractType.CALL
    option_chain = client.get_option_chain('$SPX.X', contract_type=contract_type_call, strike_count=5, include_quotes=True, from_date=startdate, to_date=enddate)
    assert option_chain.status_code == httpx.codes.OK, r.raise_for_status()

    # Extract json data
    lastPrice = 0
    option_chain = option_chain.json()
    if option_chain['status'] == 'FAILED':
        # logging.error('Retrieval of CALL options chain failed')
        make_trade = False
    else:
        lastPrice = option_chain["underlying"]["last"]

    return lastPrice


def stream_SPX_value(event, loop_timer,):
    global spx_value

    # Update SPX value using option chain
    client = create_td_client()
    while True:
        # check for stop thread event
        if event.is_set():
            print("Stopping SPX value Thread")
            return

        spx_value = retrieve_SPX_value(client)
        window['SPXValue'].update(spx_value)         # Update SPX value on the GUI       
        time.sleep(1.0)



if __name__ == '__main__':
    # Check Authorization token to see if we are near expiration
    check_auth_token()

    # Create a basic GUI window from the layout defined above
    window_title = 'Stop Loss Monitor'
    window = sg.Window(window_title, layout, size=(300, 250))

    # Events to communicate with threads
    stop_thread_event = Event()
    spx_stream_thread_event = Event()

    # We can use while loop to check for any gui_events that may occur when using the window.read() method
    while True:
        # The input data in values is a dictionary with keys specified as in the layout
        gui_event, values = window.read()

        scheduler_loop = float(values['loop_timer'])
        stop_type = values['stop_type']
        stop_trigger = values['stop_trigger']
        itm_protection_offset = values['itm_protection_offset']
        submit_stop_orders = values['submitStopOrders']

        # Create a thread to run the stop mointor
        t1 = threading.Thread(target=stop_monitor, args=(stop_thread_event, scheduler_loop, stop_type, stop_trigger, submit_stop_orders, itm_protection_offset, ))

        # Create a thread to stream SPX value
        t2 = threading.Thread(target=stream_SPX_value, args=(spx_stream_thread_event, scheduler_loop,))

        if gui_event == 'Stop':
            stop_thread(stop_thread_event)
            stop_thread(spx_stream_thread_event)

        elif gui_event == sg.WIN_CLOSED or gui_event == 'Exit':  
            stop_thread(stop_thread_event)
            stop_thread(spx_stream_thread_event)     
            break
        
        elif gui_event == 'Clear':
            clear_input()

        else :      
            #print(gui_event, values)
            stop_thread_event.clear()
            spx_stream_thread_event.clear()
            t1.start()
            t2.start()         
