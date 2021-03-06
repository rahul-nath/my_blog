"""
Replace Peewee ORM with SQLAlchemy
"""

import datetime
import functools
import os
import re
import urllib

from flask import (Flask, abort, flash, Markup, redirect, render_template,
                   request, Response, session, url_for)
from markdown import markdown
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.extra import ExtraExtension
from micawber import bootstrap_basic, parse_html
from micawber.cache import Cache as OEmbedCache
from peewee import *
from playhouse.flask_utils import FlaskDB, get_object_or_404, object_list
from playhouse.sqlite_ext import *

# hash all this shit using the session key blog post
ADMIN_PASSWORD = 'secret'
APP_DIR = os.path.dirname(os.path.realpath(__file__))
DATABASE = 'sqliteext:///%s' % os.path.join(APP_DIR, 'blog.db')
DEBUG = False
SECRET_KEY = 'shhh, secret!'  # Used by Flask to encrypt session cookie.
SITE_WIDTH = 800

app = Flask(__name__)
app.config.from_object(__name__)
flask_db = FlaskDB(app)
database = flask_db.database
oembed_providers = bootstrap_basic(OEmbedCache())

class Entry(flask_db.Model):
    title = CharField()
    slug = CharField(unique=True)
    content = TextField()
    published = BooleanField(index=True)
    # consider adding category (music, politics, philosophy, etc.)
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)

    @property
    def html_content(self):
        """
        Generate HTML representation of the markdown-formatted blog entry,
        and also convert any media URLs into rich media objects such as video
        players or images.
        """
        hilite = CodeHiliteExtension(linenums=False, css_class='highlight')
        extras = ExtraExtension()
        markdown_content = markdown(self.content, extensions=[hilite, extras])
        oembed_content = parse_html(
            markdown_content,
            oembed_providers,
            urlize_all = True,
            maxwidth=app.config['SITE_WIDTH'])
        # markup object tells flask that we trust the html content
        # so it won't be escaped
        return Markup(oembed_content)

    
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = re.sub('[^\w]+', '-', self.title.lower())
        ret = super(Entry, self).save(*args, **kwargs)

        # store the search content
        self.update_search_index()
        return ret

    def update_search_index(self):
        # Create a row in the FTSEntry table with the post content. This will
        # allow us to use SQLite's awesome full-text search extension to
        # search our entries.
        query = (FTSEntry
                 .select(FTSEntry.docid, FTSEntry.entry_id)
                 .where(FTSEntry.entry_id == self.id))
        try:
            fts_entry = FTSEntry.get(FTSEntry.entry_id == self.id)
        except FTSEntry.DoesNotExist:
            fts_entry = FTSEntry(entry_id=self.id)
            force_insert = True
        else:
            force_insert = False
        fts_entry.content = '\n'.join((self.title, self.content))
        fts_entry.save(force_insert=force_insert)

    @classmethod
    def public(cls):
        return Entry.select().where(Entry.published == True)

    @classmethod
    def drafts(cls):
        return Entry.select().where(Entry.published == False)

    @classmethod
    def search(cls, query):
        words = [word.strip() for word in query.split() if word.strip()]
        if not words:
            return Entry.select().where(Entry.id == 0)
        else:
            search = ' '.join(words)
        """
        # query the FTSEntry virtual table, which stores the search index of our blog entries
        scores = FTSEntry.select(FTSEntry, Entry, FTSEntry.rank().alias('score'))

        #  SQLite's full-text search implements a custom MATCH operator, use to match indexed content against the search query
        joined_scores = scores.join(Entry, on=(FTSEntry.entry_id == Entry.id).alias('entry'))
        
        # We also are joining the Entry table so we only return search results for published entries. 
        return joined_scores.where((Entry.published == True) & (FTSEntry.match(search))).order_by(SQL('score').desc())
        """
        return (FTSEntry
        .select(
            FTSEntry,
            Entry,
            FTSEntry.rank().alias('score'))
        .join(Entry, on=(FTSEntry.entry_id == Entry.id).alias('entry'))
        .where(
            (Entry.published == True) &
            (FTSEntry.match(search)))
        .order_by(SQL('score').desc()))


    
class FTSEntry(FTSModel):
    entry_id = IntegerField()
    content = TextField()

    class Meta:
        database = database

def login_required(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        if session.get("logged_in"):
            return fn(*args, **kwargs)
        return redirect(url_for('login', next=request.path))
    return inner

@app.route('/login/', methods=['GET', 'POST'])
def login():
    # this isn't that great
    next_url = request.args.get('next') or request.form.get('next') 
    if request.method == 'POST' and request.form.get('password'):
        password = request.form.get('password')
        # TODO: If using a one-way hash, you would also hash the user-submitted
        # password and do the comparison on the hashed versions.
        if password == app.config['ADMIN_PASSWORD']:
            session['logged_in'] = True
            session.permanent = True # using a session cookie
            flash("You are now logged in.", 'success')
            return redirect(next_url or url_for('index'))
        else:
            flash('Incorrect password', 'danger')
    return render_template('login.html', next_url=next_url)

@app.route('/logout/', methods=['GET', 'POST'])
def logout():
    if request.method == 'POST':
        session.clear()
        return redirect(url_for('login'))
    return render_template('logout.html')


@app.route('/')
def index():
    """
    This method will use the SQLite full-text search index to query 
    for matching entries. SQLite's full-text search supports boolean 
    queries, quoted phrases, and more.
    """
    search_query = request.args.get('q')
    if search_query:
        query = Entry.search(search_query)
    else:
        query = Entry.public().order_by(Entry.timestamp.desc())
    # The `object_list` helper will take a base query and then handle
    # paginating the results if there are more than 20. For more info see
    # the docs:
    # http://docs.peewee-orm.com/en/latest/peewee/playhouse.html#object_list
    return object_list("index.html", query, search=search_query, check_bounds=False)

@app.route('/drafts/')
@login_required
def drafts():
    query = Entry.drafts.order_by(Entry.timestamp.desc())
    return object_list('index.html', query)


@app.route('/create/', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        if request.form.get('title') and request.form.get('content'):
            entry = Entry.create(title=request.form['title'], content=request.form['content'], published=request.form.get('published') or False)
            flash('Entry created successfully', 'success')
            if entry.published:
                return redirect(url_for('detail', slug=entry.slug))
            else:
                return redirect(url_for('edit', slug=entry.slug))
        else:
            flash('Title and Content are required.', 'danger')
        return render_template('create.html')

@app.route('/<slug>/edit/', methods=['GET', 'POST'])
@login_required
def edit(slug):
    if request.method == 'POST':
        if request.form.get('title') and request.form.get('content'):
            entry.title = request.form['title']
            entry.content = request.form['content']
            entry.published = request.form.get('published') or False
            entry.save()

            flash('Entry saved successfully', 'success')
            if entry.published:
                return redirect(url_for('detail', slug=entry.slug))
            else:
                return redirect(url_for('edit', slug=entry.slug))
        else:
            flash('Title and Content are required.', 'danger')
    return render_template('edit.html', entry=entry)
    
@app.route('/<slug>/')
def detail(slug):
    if session.get('logged_in'):
        query = Entry.select()
    else:
        query = Entry.public()
    entry = get_object_or_404(query, Entry.slug == slug)
    return render_templates('detail.html', entry=entry)



@app.template_filter('clean_querystring')
def clean_querystring(request_args, *keys_to_remove, **new_values):
    querystring = dict((k, v) for k,v in request_args.items())
    for key in keys_to_remove():
        querystring.pop(key, None)
    querystring.update(new_values)
    return urllib.urlencode(querystring)

@app.errorhandler(404)
def not_found(exc):
    return Response('<h3>Sorry, not found</h3>'), 404

def main():
    database.create_tables([Entry, FTSEntry], safe=True)
    app.run(debug=True)


if __name__ == '__main__':
    main()
