from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import google.oauth2.id_token
from google.auth.transport import requests
import starlette.status as status
from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter, Or
from datetime import datetime
import local_constants

# define the app that will contain all of our routing for Fast API
app = FastAPI()

# Initialize Firestore client for database operations
firestore_db = firestore.Client()

# Set up request adapter for Firebase authentication
firebase_request_adapter = requests.Request()

# Define the static and templates directories
app.mount('/static', StaticFiles(directory='static'), name='static')
templates = Jinja2Templates(directory="templates")

# cloud storage code
def addDirectory(directory_name: str):
    """Add an empty directory to the cloud storage bucket.
    
    Args:
        directory_name: the name of the directory to add. Must have a trailing '/' in the name.
    """
    storage_client = storage.Client(project=local_constants.PROJECT_NAME)  # get the storage client using the specified project name
    bucket = storage_client.bucket(local_constants.PROJECT_STORAGE_BUCKET)  # list the bucket we need to use using the specified bucket name

    directory_name = directory_name + '/'  # dir names must end with a /
    # make an empty directory and upload it to the bucket
    blob = bucket.blob(directory_name)
    blob.upload_from_string("", content_type="application/x-www-form-urlencoded:charset=UTF-8")

def addFile(file, directory_name: str):
    """Add a file to the cloud storage bucket.
    
    Args:
        file: the name of the file to add.
        directory_name: the name of the directory GCS directory to upload the file to
    Returns:
        a tuple of the blob name, and the public url of the image.
    """
    storage_client = storage.Client(project=local_constants.PROJECT_NAME)  # get the storage client using the specified project name
    bucket = storage_client.bucket(local_constants.PROJECT_STORAGE_BUCKET)  # list the bucket we need to use using the specified bucket name

    blob = bucket.blob(directory_name + '/' + file.filename)
    blob.upload_from_file(file.file)
    return (directory_name + '/' + file.filename, blob.public_url)

def deleteFile(blob_name:  str):
    """Delete a file in the cloud storage bucket.
    
    Args:
        blob_name: the name of the blob to be deleted in GCS
    """
    storage_client = storage.Client(project=local_constants.PROJECT_NAME)  # get the storage client using the specified project name
    bucket = storage_client.bucket(local_constants.PROJECT_STORAGE_BUCKET)  # list the bucket we need to use using the specified bucket name
    
    bucket.delete_blob(blob_name)

def getUser(user_token):
    """Function to retrieve user data from Firestore or create a default user if not found.
    
    If the user does not exist, create a document with blank fields.
    """
    user = firestore_db.collection('User').document(user_token['user_id'])
    if not user.get().exists:
        user_data = {
            "username": "",  # username for this user
            "tweets": [],  # a list of the tweets associated with this user
            "following": [],  # a list of usernames of the users this user follows
            "followers": [],  # a list of usernames of the users who follow this user
        }
        firestore_db.collection('User').document(user_token['user_id']).set(user_data)
    return user

def validateFirebaseToken(id_token):
    """Function to validate Firebase ID token and retrieve user information."""
    if not id_token:
        return None
    
    user_token = None
    try:
        user_token = google.oauth2.id_token.verify_firebase_token(id_token, firebase_request_adapter)
    except ValueError as err:
        print(str(err))

    return user_token

def sort_tweets(tweet):
    """Return the key for sorting the tweets according to the date."""
    try:
        return tweet.get("date")
    except ValueError:
        return tweet.get().get("date")

async def generate_timeline(user_token):
    """Generate a timeline for the user of the 20 latest tweets from the user's tweets and the people the user is following."""
    user = firestore_db.collection('User').document(user_token['user_id'])
    following = user.get().get("following")
    following.append(user.get().get("username"))
    tweets_ref = firestore_db.collection('Tweet')
    tweets_query = tweets_ref.where("username", "in", following).order_by("username", direction=firestore.Query.ASCENDING).order_by("date", direction=firestore.Query.DESCENDING)

    tweets = tweets_query.get()
    sorted_tweets = sorted(tweets, key=sort_tweets, reverse=True)
    #return reversed(sorted_tweets[-20:])
    return sorted_tweets[:19]

@app.get('/set-username', response_class=HTMLResponse)
async def setUsername(request: Request):
    """Route (GET) for setting the username when a user logs in for the first time."""
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        return templates.TemplateResponse('main.html', {"request": request, "user_token": None, "error_message": None, "user_info": None})
    
    user = getUser(user_token).get()

    context_dict = dict(
        request=request,
        user_token=user_token,
        error_message=error_message,
        user_info=user,
    )

    return templates.TemplateResponse('set-username.html', context=context_dict)

@app.post('/set-username', response_class=HTMLResponse)
async def setUsername(request: Request):
    """Route (POST) for setting the username.
    
    If the username is taken, redisplay the form with the error message.
    """
    id_token = request.cookies.get("token")
    user_token = None

    user_token = validateFirebaseToken(id_token)

    form = await request.form()

    user_exists = firestore_db.collection("User").where(filter=FieldFilter('username', '==', form['username'])).get()
    if user_exists:
        errors = ['This username is already taken.']
        return templates.TemplateResponse('set-username.html', {"request": request, "user_token": None, "errors": errors, "user_info": None,})
    firestore_db.collection('User').document(user_token['user_id']).update({"username": form["username"]})
    addDirectory(form['username'])
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Route (GET) for the main page, handling user authentication, setting username for first time login, and displaying timeline."""
    # Query firebase for the request token. An error message is set in case we want to output an error to 
    # the user in the template.
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=None,
            errors=errors,
            user_info=None
        )
        return templates.TemplateResponse('main.html', context=context)
    
    user = getUser(user_token).get()
    if not user.get("username"):
        context = dict(
            request=request,
            user_token=user_token,
            errors=errors,
            user_info=user
        )
        return templates.TemplateResponse('set-username.html', context=context)

    context = dict(
        request=request,
        user_token=user_token,
        errors=errors,
        user_info=user,
        tweets= await generate_timeline(user_token)
    )

    return templates.TemplateResponse('main.html', context=context)

@app.get("/profile", response_class=HTMLResponse)
async def viewYourProfile(request: Request):
    """Route (GET) for displaying the user's own profile."""
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=None,
            errors=errors,
            user_info=None
        )
        return templates.TemplateResponse('main.html', context=context)
    
    user = getUser(user_token).get()

    context = dict(
        request=request,
        user_token=user_token,
        error=errors,
        user_info=user,
        personal_info=user.get("username"),
        following=len(user.get("following")),
        followers=len(user.get("followers")),
        tweets=sorted(user.get('tweets'), key=sort_tweets, reverse=True),
    )
    return templates.TemplateResponse('view-profile.html', context=context)

@app.get("/post", response_class=HTMLResponse)
async def addTweet(request: Request):
    """Route (GET) for adding a tweet.

    Return the html form where user types the tweet.
    """
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)
    user = getUser(user_token).get()

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=user_token,
            error_message=errors,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context)
    
    context = dict(
        request=request,
        user_token=user_token,
        errors=None,
        user_info=user,
    )

    return templates.TemplateResponse('add-tweet.html', context=context)

@app.post("/post", response_class=HTMLResponse)
async def addTweet(request: Request):
    """Route (POST) for adding a tweet.

    Saves the associated tweet to the db.
    """
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)
    user = getUser(user_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=user_token,
            error_message=errors,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context)

    user_name = user.get().get("username")
    image_url = ''
    blob_name = ''

    form = await request.form()
    if form['tweetImage'] and form['tweetImage'].filename:
        blob_name, image_url = addFile(form["tweetImage"], user_name)
    
    tweet_data = {
        'username': user_name,  # the username of the user who posted the tweet
        'date': datetime.strptime(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S"),  # a timestamp of the time the tweet was posted
        'body': form['tweet'],  # the main content of the tweet
        'image_url': image_url,  # public url of the image associated with this tweet
        'blob_name': blob_name,  # name of the blob in the bucket, to be used when deleting an image
    }
    tweet_ref = firestore_db.collection('Tweet').document()
    tweet_ref.set(tweet_data)
    tweets_list = user.get().get('tweets')
    tweets_list.append(tweet_ref)
    user.update({'tweets': tweets_list})
    
    return RedirectResponse('/', status_code=status.HTTP_302_FOUND)

@app.post("/search-username", response_class=HTMLResponse)
async def searchUsername(request: Request):
    """Route (POST) for searching a username in the database."""
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=user_token,
            errors=errors,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context)
    
    form = await request.form()
    username_query = form['username']

    user = getUser(user_token).get()
    matched_users = []
    for user in firestore_db.collection('User').stream():
        if user.get('username')[:len(username_query)].lower() == username_query.lower():
            matched_users.append(user)

    context = dict(
        request=request,
        user_token=user_token,
        errors=errors,
        user_info=user,
        user_results=matched_users,
    )

    return templates.TemplateResponse('user-search-results.html', context=context)

@app.post("/search-tweet", response_class=HTMLResponse)
async def searchTweet(request: Request):
    """Route (POST) for searching content in tweets."""
    id_token = request.cookies.get("token")
    errors: str | None = None
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context = dict(
            request=request,
            user_token=user_token,
            error_message=errors,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context)
    
    form = await request.form()
    content_query = form['content']

    user = getUser(user_token).get()
    matched_content = [tweet for tweet in firestore_db.collection('Tweet').stream() if tweet.get('body')[:len(content_query)].lower() == content_query.lower()]

    context = dict(
        request=request,
        user_token=user_token,
        errors=None,
        user_info=user,
        tweet_results=sorted(matched_content, key=sort_tweets, reverse=True),
    )
    return templates.TemplateResponse('tweet-search-results.html', context=context)

@app.get("/view-profile/{person}", response_class=HTMLResponse)
async def viewOthersProfile(request: Request, person):
    """Route (GET) for viewing the profile of another user given their username.
    
    Args:
        person -> str: the username of the user's profile to view.
    """
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=error_message,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)
    
    user = getUser(user_token).get()
    *_, person_query = firestore_db.collection('User').where(filter=FieldFilter('username', '==', person)).get()

    context_dict = dict(
        request=request,
        user_token=user_token,
        error_message=error_message,
        user_info=user,
        personal_info=person_query.get("username"),
        is_following=person in user.get("following"),
        following=len(person_query.get("following")),
        followers=len(person_query.get("followers")),
        tweets=sorted(person_query.get("tweets")[-10] if len(person_query.get('tweets')) > 10 else person_query.get('tweets'), key=sort_tweets, reverse=True),
    )
    return templates.TemplateResponse('view-profile.html', context=context_dict)

@app.post("/follow/{person}", response_class=HTMLResponse)
async def follow(request: Request, person):
    """Route (POST) for following another user.
    Args:
        person -> str: the username of the user to follow.
    """
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=error_message,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)
    
    user = getUser(user_token)
    *_, person_query = firestore_db.collection('User').where(filter=FieldFilter('username', '==', person)).get()  # query the user snapshot document matching the name given, and unpack the list elements taking only the last element and discarding the rest
    person_followers = person_query.get("followers")  # get the followers list associated with the user
    person_followers.append(user.get().get("username"))  # add a new element to the list of followers
    person_query.reference.update({"followers": person_followers})  # on the user snapshot document, get the reference to the DocumentReference which will be used to update the list of followers on the user document
    following_list = user.get().get("following")  # get the list of the users the signed in user is following
    following_list.append(person)  # add a new element to this list for the other user to start following
    user.update({'following': following_list})  # update the list of following users in the user document

    return RedirectResponse(f"/view-profile/{person}/", status_code=status.HTTP_302_FOUND)

@app.post("/unfollow/{person}", response_class=HTMLResponse)
async def unfollow(request: Request, person):
    """Route (POST) for unfollowing a followed user.
    
    Args:
        person -> str: the username of the user to unfollow.
    """
    id_token = request.cookies.get("token")
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=None,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)

    user = getUser(user_token)
    *_, person_query = firestore_db.collection('User').where(filter=FieldFilter('username', '==', person)).get()  # query the user snapshot document matching the name given, and unpack the list elements taking only the last element and discarding the rest
    person_followers = person_query.get("followers")  # get the followers list associated with the user
    person_followers.remove(user.get().get("username"))  # remove the username of this user from the list of followers
    person_query.reference.update({"followers": person_followers})  # on the user snapshot document, get the reference to the DocumentReference which will be used to update the list of followers on the user document
    following_list = user.get().get("following")  # get the list of the users the signed in user is following
    following_list.remove(person)   # add a new element to this list for the other user to start following
    user.update({'following': following_list})  # update the list of following users in the user document

    return RedirectResponse(f"/view-profile/{person}/", status_code=status.HTTP_302_FOUND)

@app.get('/edit-tweet/{tweet_index}', response_class=HTMLResponse)
async def editTweet(request: Request, tweet_index):
    """Route (GET) for editing a tweet.
    
    Args:
        tweet_index -> str: the index of the tweet in the `tweets` list associated with each user account.

    Return a form prefilled with the current tweet content."""
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=error_message,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)
    
    user = getUser(user_token).get()

    context_dict = dict(
        request=request,
        user_token=user_token,
        error_message=error_message,
        user_info=user,
        index=len(user.get("tweets")) - 1 - int(tweet_index),
        tweet=user.get("tweets")[len(user.get("tweets")) - 1 - int(tweet_index)],
    )
    
    return templates.TemplateResponse('edit-tweet.html', context=context_dict)

@app.post('/edit-tweet', response_class=HTMLResponse)
async def editTweet(request: Request):
    """Route (POST) for editing a tweet.
    
    Saves the updated tweet content to the datatbase.
    """
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=error_message,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)
    
    user = getUser(user_token)
    users_tweets = user.get().get('tweets')

    form = await request.form()
    tweet_index = int(form['index'])
    updated_tweet = form['tweet']
    image_url = ''
    blob_name = users_tweets[tweet_index].get().get("blob_name")

    if form['tweetImage'] and form['tweetImage'].filename:
        """A new image has been uploaded with the tweet"""
        if blob_name:
            deleteFile(blob_name)  # delete the old image associated with this tweet
        blob_name, image_url = addFile(form["tweetImage"], user.get().get("username"))  # set the new image
        tweet_data = {
            'body': updated_tweet,
            'image_url': image_url,
            'blob_name': blob_name,
        }
    else:
        tweet_data = {
            'body': updated_tweet
        }
    
    users_tweets[tweet_index].update(tweet_data)

    context_dict = dict(
        request=request,
        user_token=user_token,
        error_message=error_message,
        user_info=user.get(),
        personal_info=user.get().get("username"),
        following=len(user.get().get("following")),
        followers=len(user.get().get("followers")),
        tweets=user.get().get('tweets'),
    )
    return templates.TemplateResponse('view-profile.html', context=context_dict)

@app.post('/delete-tweet', response_class=HTMLResponse)
async def deleteTweet(request: Request):
    """Route (POST) for deleting a tweet.
    
    Deletes the tweet from the database and from the `tweets` list associated with the user.
    """
    id_token = request.cookies.get("token")
    error_message = "No error here"
    user_token = None
    user = None

    user_token = validateFirebaseToken(id_token)

    # Validate user token - check if we have a valid firebase login if not return the template with empty data as we will show the login box
    if not user_token:
        context_dict = dict(
            request=request,
            user_token=user_token,
            error_message=error_message,
            user_info=user,
        )
        return templates.TemplateResponse('main.html', context=context_dict)
    
    form = await request.form()
    
    user = getUser(user_token)
    tweet_index = len(user.get().get("tweets")) - 1 - int(form['index'])
    user_tweets = user.get().get('tweets')

    blob_name = user_tweets[tweet_index].get().get("blob_name")
    if blob_name:
        """The tweet has an image associated with it, delete the image too."""
        deleteFile(blob_name)  # delete the old image associated with this tweet

    user_tweets[tweet_index].delete()
    del user_tweets[tweet_index]
    user.update({'tweets': user_tweets})

    context_dict = dict(
        request=request,
        user_token=user_token,
        error_message=error_message,
        user_info=user.get(),
        personal_info=user.get().get("username"),
        following=len(user.get().get("following")),
        followers=len(user.get().get("followers")),
        tweets=user.get().get('tweets'),
    )
    return templates.TemplateResponse('view-profile.html', context=context_dict)