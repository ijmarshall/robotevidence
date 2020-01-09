from flask import Flask, render_template, send_file
from flask import request
from trialstreamer import dbutil
import trialstreamer
import datetime
import os
import humanize
from flask import json
import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json
from collections import defaultdict
import pickle
import pygtrie
from flask import jsonify
from io import BytesIO as StringIO # py3

from flask_cors import CORS

app = Flask(__name__)
CORS(app)




with open(os.path.join(trialstreamer.DATA_ROOT, 'rct_model_calibration.json'), 'r') as f:
    clf_cutoffs = json.load(f)

with open(os.path.join(trialstreamer.DATA_ROOT, 'pico_mesh_autocompleter.pck'), 'rb') as f:
    pico_trie = pickle.load(f)


@app.route('/')
def hello_world():
    return 'trialstreamer :)'


@app.route('/pico_term_lookup')
def pico_term_lookup():
    """
    retrieves most likely MeSH PICO terms for the demo
    """
    min_char = 3
    max_return = 5
    substr = request.args.get('q')
    if substr is None:
        return jsonify([])

    matches = pico_trie.itervalues(prefix=substr)

    if len(substr) < min_char:
        # for short ones just return first 5
        return jsonify([r for _, r in zip(range(max_return), matches)])
    else:
        # where we have enough chars, process and get top ranked
        return jsonify(sorted(matches, key=lambda x: x['count'], reverse=True)[:max_return])




@app.route('/pico_mesh_query', methods=["POST"])
def pico_mesh_query():
    """
    gets up to 10 articles matching a structured PICO query
    """
    query = request.get_json('q')
    if query is [] or query is None:
        return jsonify([])

    builder = []

    for c in query:
        assert c['classes'] in ['population', 'interventions', 'outcomes']
        field = sql.SQL('.').join((sql.Identifier("pa"), sql.Identifier(f"{c['classes']}_mesh")))
        contents = sql.Literal(Json([{"mesh_ui": c['mesh_ui']}])                           )
        builder.append(sql.SQL(' @> ').join((field, contents)))

    params = sql.SQL(' AND ').join(builder)

    select = sql.SQL("SELECT pm.pmid, pm.ti FROM pubmed as pm, pubmed_annotations as pa WHERE ")
    join = sql.SQL("AND pm.pmid = pa.pmid")
                                                                        
    out = []
                                                                        
    with dbutil.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor, name="pico_mesh") as cur:
        cur.execute(select + params + join)
        for i, row in enumerate(cur):
            out.append({"pmid": row['pmid'], "ti": row['ti']})
            if i>5:
                break

    return jsonify(out)

# @app.route('/query')
# def query():
#     """
#     emulated PubMed-style boolean query
#     """
#     # still to improve!!

#     q = request.args.get('q', '')
#     q = pmquery_to_postgres(q)


@app.route('/status')
def status():
    """
    Retrieves most recent update dates
    """
    update_types = ["ictrp", "pubmed_baseline", "pubmed_update", "picospan_partial", "picospan_full", "picomesh_partial", "picomesh_full"]

    out = []

    for t in update_types:
        lu = dbutil.last_update(t)
        if lu:
            lu['age'] = humanize.naturaldelta(datetime.datetime.now() - lu['source_date'])
            out.append(lu)

    return render_template('status.html', results=out)


# @app.route('/query')


@app.route('/rcts')
def rcts():
    """
    Displays info about RCTs in database
    """

    # select count(*) from pubmed where pm_data @> '{"ptyp": ["Randomized Controlled Trial"]}';


    threshold_types = ["precise", "balanced", "sensitive"]
    threshold_colors = ["#004c6d", "#6996b3", "#c1e7ff", "#ffa600", "#ff6361"]

    #threshold_colors = ["#004c6d", "#ffa600"]

    global clf_cutoffs

    cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.DictCursor)

    totals = []

    breakdowns = defaultdict(dict)

    cur.execute("select * from pubmed_year_counts order by year;")
    records = cur.fetchall()
    for r in records:
        breakdowns[r['year']]["PubMed PT tag"] = r['ptyp_rct']
        breakdowns[r['year']]["precise"] = r['is_rct_precise']
        breakdowns[r['year']]["balanced"] = r['is_rct_balanced']
        breakdowns[r['year']]["sensitive"] = r['is_rct_sensitive']

    cur.execute("select year, count(*)*avg(rct_probability) as prob_count from pubmed group by year;")
    records = cur.fetchall()
    for r in records:
        breakdowns[r['year']]["sensitive"] = r['prob_count']



    cur.execute("select sum(is_rct_precise) as is_rct_precise, sum(is_rct_balanced) as is_rct_balanced, sum(is_rct_sensitive) as is_rct_sensitive, sum(ptyp_rct) as ptyp_rct from pubmed_year_counts;")
    record = cur.fetchone()
    totals.append({"count": record['is_rct_precise'], "threshold_type": "precise"})
    totals.append({"count": record['is_rct_balanced'], "threshold_type": "balanced"})
    totals.append({"count": record['is_rct_sensitive'], "threshold_type": "sensitive"})
    totals.append({"count": record['ptyp_rct'], "threshold_type": "PubMed PT tag"})




    # # add all non-ptyp estimates
    # cur.execute("select year, count(*) from pubmed where score_svm_cnn>={} group by year;".format(clf_cutoffs['thresholds']['svm_cnn']['precise']))
    # records = cur.fetchall()
    # for r in records:
    #     breakdowns[r['year']]['non_ptyp'] = r['count']


#        cur.execute("select year, count(*) from pubmed where (clf_type='svm_cnn' and is_rct_{}=true) group by year;".format(t))
#        records = cur.fetchall()
#        for r in records:
#            breakdowns[r['year']]["{} no ptyp".format(t)] = r['count']
#


    cur.execute("select count(*), year from ictrp group by year;")
    records = cur.fetchall()
    for r in records:
        breakdowns[r['year']]["Trial registries"] = r['count']


    # breakdowns.pop('')
    breakdowns.pop(None)

    # for now remove blank years, but there are a few thousand in that group

    # breakdowns_ptyp.pop('')
    # breakdowns_ptyp.pop(None)


    year_labels = [y for y in sorted(breakdowns.keys()) if y and y >= 1950 and y <=2019]
    datasets = []

    hide_on_load = [False, True, True, False, True, True]


    for tl, t, tc, hl in zip(['RobotReviewer ML classifier (precise)', 'RobotReviewer ML classifier (balanced)', 'RobotReviewer ML classifier (sensitive)',  'PubMed PT tag', 'Trial registries'], threshold_types + ["PubMed PT tag", 'Trial registries'], threshold_colors, hide_on_load):
        datasets.append({"label": tl, "hidden": hl, "data": [breakdowns[y].get(t, 0) for y in year_labels], "fill": False, "borderColor": tc, "borderWidth": 2})

    # format year labels
    year_labels = [y if (int(y) % 2) == 0 else "" for y in year_labels]
    cur.close()
    return render_template('rcts.html', totals=totals, labels=json.dumps(year_labels), datasets=json.dumps(datasets))







@app.route('/db_dump')
def db_dump():
    """
    dumps the full database
    TODO figure out whether to do as a pgdump, or as
    a custom standardised data format
    """
    cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT pmid FROM pubmed WHERE is_rct_precise=true;")
    records = cur.fetchall()
    pmids = [r['pmid'] for r in records]
    report = json.dumps(pmids)
    strIO = StringIO()
    strIO.write(report.encode('utf-8')) # need to send as a bytestring
    strIO.seek(0)
    return send_file(strIO,
                     attachment_filename="rct_pmids.json",
                     as_attachment=True)


# def pmquery_to_postgres(s):
#     s = s.replace(' and ', ' & ')
#     s = s.replace(' AND ', ' & ')
#     s = s.replace(' or ', ' | ')
#     s = s.replace(' OR ', ' | ')
#     return s

# @app.route('/query')
# def query():
#     """
#     emulated PubMed-style boolean query
#     """
#     # still to improve!!

#     q = request.args.get('q', '')
#     q = pmquery_to_postgres(q)


#     threshold_type = request.args.get('threshold_type', 'balanced')
#     start = int(request.args.get('start', 0))
#     end = int(request.args.get('end', start+10))
#     cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.DictCursor)
#     cur.execute("SELECT pm_data FROM pubmed WHERE ti @@ to_tsquery(%s) limit %s offset %s;", (q, end-start, int(start)))
#     records = cur.fetchall()
#     return jsonify(records)


@app.route('/get')
def get():
    """
    returns record or records matching id or list of ids
    by tsid, pmid, or regid params
    """
    return 'get placeholder'


@app.route('/fuzzy')
def fuzzy():
    """
    fuzzy matcher
    pass some combination of identifiable article parms
    e.g. title, pdf md5 hash, journal, citation fields

    returns single match only if high certainty, else none
    """
    return 'fuzzy matcher placeholder'



def main():
    debug = os.environ.get('APP_DEBUG', True)
    host = os.environ.get('APP_HOST', '0.0.0.0')
    port = int(os.environ.get('APP_PORT', 5000))
    app.run(debug=debug, host=host, port=port)


if __name__ == '__main__':
    main()
