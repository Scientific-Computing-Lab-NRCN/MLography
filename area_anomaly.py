import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore",category=FutureWarning)
    import numpy as np
    import statistics
    from utils import impurity_dist, num_threads, find_diameter
    import ray
    import time
    import json
    import matplotlib.pyplot as plt
    import matplotlib
    import cv2 as cv
    import os


class CheapImpCouple:
    def __init__(self, containing_cluster_inside):
        self.cheapest_impurity_outside = None
        self.containing_cluster_outside = None
        self.cheapest_impurity_inside = None
        self.containing_cluster_inside = containing_cluster_inside
        self.lowest_price = np.inf

    def update_cheapest_couple(self, cheap_imp_in, cheap_imp_out, containing_cluster_out, cheap_price):
        if cheap_price < self.lowest_price:
            self.cheapest_impurity_inside = cheap_imp_in
            self.cheapest_impurity_outside = cheap_imp_out
            self.containing_cluster_outside = containing_cluster_out
            self.lowest_price = cheap_price

    def merge_cheapest_couples(self, couples_list):
        for couple in couples_list:
            self.update_cheapest_couple(couple.cheapest_impurity_inside, couple.cheapest_impurity_outside,
                                        couple.containing_cluster_outside, couple.lowest_price)


class MarketClustering:

    def __init__(self, img_shape, indices, markers, imp_boxes, anomaly_scores, k=10):
        self.img_shape = img_shape
        self.indices = indices
        self.markers = markers
        self.imp_boxes = imp_boxes
        self.anomaly_scores = anomaly_scores
        self.k = k
        self.anomaly_clusters = [None] * self.k  # create k clusters
        self.sorted_impurities = []
        self.auction_impurities = {}
        self.init_clusters()

    def init_clusters(self):
        dtype = [('id', int), ('score', float)]
        scores_with_impurity_id = np.array([(i, self.anomaly_scores[i]) for i in range(len(self.anomaly_scores))
                                            if self.anomaly_scores[i] > 0], dtype=dtype)  # ignore impurities with score 0
        sorted_impurities = np.sort(scores_with_impurity_id, order='score')  # order the impurities by their scores
        self.sorted_impurities = [impurity for (impurity, score) in sorted_impurities]

        for cluster in range(self.k):
            imp_id = 1 + cluster
            self.anomaly_clusters[cluster] = {}
            core_impurity = self.sorted_impurities[-imp_id]
            # set the core impurities with highest impurities
            self.anomaly_clusters[cluster]["core_impurities"] = [core_impurity]
            # set initial clusters with highest impurities
            self.anomaly_clusters[cluster]["impurities_inside"] = [core_impurity]
            # set initial wallet for each cluster
            # self.anomaly_clusters[cluster]["wallet"] = (self.anomaly_scores[core_impurity] * 1e4) ** 2.7
            self.anomaly_clusters[cluster]["wallet"] = np.exp(np.sqrt(self.anomaly_scores[core_impurity] * 1e2)) ** 2.8
            # self.anomaly_clusters[cluster]["wallet"] = (self.anomaly_scores[core_impurity] * 1e2) ** 5
            # set initial anomaly score for the cluster. updated only  in update_clusters_scores
            self.anomaly_clusters[cluster]["order_keys"] = []

    def find_containing_cluster(self, impurity):
        """
        Returns the index of the cluster that currently contains the impurity, together with a boolean value that is True if
        the given impurity is a core impurity of that cluster, or False otherwise. Note that there may be only one cluster
        containing each impurity in a given time
        """
        for cluster in self.anomaly_clusters:
            if impurity in cluster["core_impurities"]:
                return cluster, True
            if impurity in cluster["impurities_inside"]:
                return cluster, False
        return -1, False

    def find_cheapest_imp_in_cluster(self, cluster, impurity, is_core_impurity_out):
        """

        :param cluster: cluster in which the cheapest impurity is being searched
        :param impurity: the impurity outside the cluster that searches for cheapest impurity inside the cluster
        :return: the cheapest impurity inside the cluster, and its price
        """
        lowest_price = np.inf
        cheapest_impurity = None
        for impurity_inside in cluster["impurities_inside"]:
            is_core_impurity_inside = True if impurity_inside in cluster["core_impurities"] \
                else False

            distance = impurity_dist(self.imp_boxes[impurity], self.imp_boxes[impurity_inside])
            f = 0.95
            scores_part = (1 - (self.anomaly_scores[impurity] * f) ** 0.5 *
                           (self.anomaly_scores[impurity_inside] * f) ** 0.5) ** 1.6
            distance_part = np.exp(np.sqrt(distance)) ** 1.7
            price = distance_part * scores_part

            # penalty = (2 - np.abs(self.anomaly_scores[impurity] - self.anomaly_scores[impurity_inside])) ** 8
            # price *= penalty

            # if is_core_impurity_out and is_core_impurity_inside:
            #     # discount for cluster combining
            #     discount_part = (1 - (self.anomaly_scores[impurity] * f) ** 0.05 *
            #                      (self.anomaly_scores[impurity_inside] * f) ** 0.05) ** 2
            #     price *= discount_part
            if is_core_impurity_out:
                # discount for cluster combining
                discount_part = (1 - (self.anomaly_scores[impurity] * f) ** 0.05 *
                                 (self.anomaly_scores[impurity_inside] * f) ** 0.05) ** 2.5
                price *= discount_part
                penalty = (2 - np.abs(self.anomaly_scores[impurity] - self.anomaly_scores[impurity_inside])) ** 8
                price *= penalty


            if price < lowest_price:
                #  ignore impurities of bigger bidders
                if impurity not in self.auction_impurities or self.auction_impurities[impurity] < cluster["wallet"]:
                    lowest_price = price
                    cheapest_impurity = impurity_inside
            return cheapest_impurity, lowest_price

    def attempt_to_expand(self, containing_cluster, impurity, cheapest_impurity, lowest_price, cluster):
        """
        Attempts to expand given cluster with the cheapest_impurity
        :param containing_cluster: the containing cluster of the impurity that is being added to the cluster
        :param impurity: the impurity that is being added to the cluster
        :param cheapest_impurity: the cheapest impurity for the impurity in the cluster that is being expanded
        :param lowest_price: the price of the cheapest impurity
        :param cluster: the cluster that is being expanded
        :return: a status code: 0 - nothing has changed (the cluster can't afford addind the cheapest impurity),
        1 - the cluster added the impurity and the impurity is not the core_impurity of the cluster
        2 - the cluster added the impurity and the impurity is the core_impurity of the cluster (both clusters are combined into one)
        """
        if containing_cluster != -1 and impurity in containing_cluster["core_impurities"]:
            self.auction_impurities[impurity] = cluster["wallet"]
            cluster["wallet"] += containing_cluster["wallet"]
            cluster["core_impurities"].extend(containing_cluster["core_impurities"])
            cluster["impurities_inside"].extend(containing_cluster["impurities_inside"])
            self.anomaly_clusters.remove(containing_cluster)
            return 2
        else:
            if cluster["wallet"] >= lowest_price:
                self.auction_impurities[impurity] = cluster["wallet"]
                cluster["wallet"] -= lowest_price
                cluster["impurities_inside"].append(impurity)
                if containing_cluster != -1:
                    containing_cluster["impurities_inside"].remove(impurity)
                return 1
        return 0

    @ray.remote
    def make_clusters_single(self, cluster, impurities_not_in_cluster_chunk):
        cheapest_impurity_couple = CheapImpCouple(cluster)
        for impurity in impurities_not_in_cluster_chunk:
            containing_cluster, is_core_impurity = self.find_containing_cluster(impurity)
            #  calculate prices for all impurities in cluster to all impurities not in cluster,
            #  choose to add best one.

            cheap_impurity_inside, cheap_price_inside = self.find_cheapest_imp_in_cluster(cluster, impurity,
                                                                                          is_core_impurity)
            cheapest_impurity_couple.update_cheapest_couple(cheap_impurity_inside, impurity, containing_cluster,
                                                            cheap_price_inside)
        return cheapest_impurity_couple

    def make_clusters(self):
        start = time.time()
        # converged = False
        status = -1
        while status != 0:
            # converged = True
            status = 0
            self.anomaly_clusters.sort(key=lambda x: x["wallet"], reverse=True)
            for cluster in self.anomaly_clusters:
                if status == 2:  # clusters where combined, need to sort the clusters in the outer loop
                    break
                cheapest_impurity_couple = CheapImpCouple(cluster)
                impurities_not_in_cluster = list(set(list(self.sorted_impurities)) - set(cluster["impurities_inside"]))
                impurities_not_in_cluster_chunks = np.array_split(impurities_not_in_cluster, num_threads)

                tasks = list()
                for i in range(num_threads):
                    tasks.append(self.make_clusters_single.remote(self, cluster, impurities_not_in_cluster_chunks[i]))
                couples_list = list()
                for i in range(num_threads):
                    couples_list.append(ray.get(tasks[i]))

                cheapest_impurity_couple.merge_cheapest_couples(couples_list)

                status = self.attempt_to_expand(
                    cheapest_impurity_couple.containing_cluster_outside,
                    cheapest_impurity_couple.cheapest_impurity_outside,
                    cheapest_impurity_couple.cheapest_impurity_inside,
                    cheapest_impurity_couple.lowest_price,
                    cluster)
        end = time.time()
        print("time make_clusters parallel: " + str(end - start))

    def make_clusters_not_parallel(self):
        # converged = False
        status = -1
        while status != 0:
            # converged = True
            status = 0
            # self.color_clusters()
            self.anomaly_clusters.sort(key=lambda x: x["wallet"], reverse=True)
            for cluster in self.anomaly_clusters:
                if status == 2:   # clusters where combined, need to sort the clusters in the outer loop
                    break
                impurities_not_in_cluster = set(list(self.sorted_impurities)) - set(cluster["impurities_inside"])
                cheapest_impurity_outside = None
                containing_cluster_outside = None
                cheapest_impurity_inside = None
                lowest_price = np.inf
                for impurity in impurities_not_in_cluster:
                    containing_cluster, is_core_impurity = self.find_containing_cluster(impurity)
                    #  calculate prices for all impurities in cluster to all impurities not in cluster,
                    #  choose to add best one.

                    cheap_impurity_inside, lowest_price_inside = self.find_cheapest_imp_in_cluster(cluster, impurity)
                    if lowest_price_inside < lowest_price:
                        cheapest_impurity_inside = cheap_impurity_inside
                        cheapest_impurity_outside = impurity
                        containing_cluster_outside = containing_cluster
                        lowest_price = lowest_price_inside

                status = self.attempt_to_expand(
                    containing_cluster_outside, cheapest_impurity_outside, cheapest_impurity_inside, lowest_price,
                    cluster)

    def update_clusters_score(self, areas=None, imp_boxes=None):
        clusters_order_in_scan = []
        for cluster in self.anomaly_clusters:
            cluster_anomaly_scores = [self.anomaly_scores[i] for i in cluster["impurities_inside"]]

            cluster["order_keys"].append({"name": "median", "score": statistics.median(cluster_anomaly_scores)})
            cluster["order_keys"].append({"name": "mean", "score": statistics.mean(cluster_anomaly_scores)})
            cluster["order_keys"].append({"name": "sum", "score": sum(cluster_anomaly_scores)})
            amount = len(cluster_anomaly_scores)
            cluster["order_keys"].append({"name": "amount", "score": amount})

            if areas is not None:
                areas_inside = [areas[i] for i in cluster["impurities_inside"]]
                cluster["order_keys"].append({"name": "areas_sum", "score": sum(areas_inside)})

            if imp_boxes is not None:
                boxes_inside = [imp_boxes[i] for i in cluster["impurities_inside"]]
                diameter = find_diameter(boxes_inside)
                cluster["order_keys"].append({"name": "diameter", "score": diameter})
                if diameter != 0:
                    cluster["order_keys"].append({"name": "amount_div_diameter", "score": amount / diameter})
                    cluster["order_keys"].append({"name": "sum_div_diameter", "score": sum(cluster_anomaly_scores)
                                                                                       / diameter})
                else:
                    cluster["order_keys"].append({"name": "amount_div_diameter", "score": -1})
                    cluster["order_keys"].append({"name": "sum_div_diameter", "score": -1})


            if areas is not None and imp_boxes is not None:
                if diameter != 0:
                    cluster["order_keys"].append({"name": "area_sum_div_diameter", "score": sum(areas_inside)/diameter})
                else:
                    cluster["order_keys"].append({"name": "area_sum_div_diameter", "score": -1})
                cluster["order_keys"].append({"name": "area_sum_mult_diameter", "score": sum(areas_inside) * diameter})
                anomaly_areas_scores = [self.anomaly_scores[i] * areas[i] for i in cluster["impurities_inside"]]
                cluster["order_keys"].append({"name": "weighted_area_sum_mult_diameter",
                                              "score": sum(anomaly_areas_scores) * diameter})
                anomaly_areas_scores = [self.anomaly_scores[i] ** 2 * areas[i] for i in cluster["impurities_inside"]]
                cluster["order_keys"].append({"name": "weighted2_area_sum_mult_diameter",
                                              "score": sum(anomaly_areas_scores) * diameter})
                anomaly_areas_scores = [self.anomaly_scores[i] * areas[i] ** 2 for i in cluster["impurities_inside"]]
                cluster["order_keys"].append({"name": "weighted_area2_sum_mult_diameter",
                                              "score": sum(anomaly_areas_scores) * diameter})
                weighted_area2_sum_mult_diameter_mult_amount = sum(anomaly_areas_scores) * diameter * amount
                cluster["order_keys"].append({"name": "weighted_area2_sum_mult_diameter_mult_amount",
                                              "score": weighted_area2_sum_mult_diameter_mult_amount})
                clusters_order_in_scan.append(weighted_area2_sum_mult_diameter_mult_amount)

                anomaly_areas_scores = [self.anomaly_scores[i] * areas[i] for i in cluster["impurities_inside"]]
                cluster["order_keys"].append({"name": "weighted2_area2_sum_mult_diameter",
                                              "score": sum(np.array(anomaly_areas_scores) ** 2) * diameter})
                cluster["order_keys"].append({"name": "weighted_area_sum2_mult_diameter",
                                              "score": sum(np.array(anomaly_areas_scores)) ** 2 * diameter})
                cluster["order_keys"].append({"name": "weighted_area_sum_mult_diameter2",
                                              "score": sum(np.array(anomaly_areas_scores)) * diameter ** 2})
        indices = np.argsort(clusters_order_in_scan)
        self.anomaly_clusters = [self.anomaly_clusters[indices[i]] for i in range(len(self.anomaly_clusters))]

    def write_clusters_score(self, scan_name, log_path, plots_dir):
        if not os.path.exists(log_path):
            os.mknod(log_path)
        if not os.path.exists(plots_dir):
            os.makedirs(plots_dir)
        with open(log_path, "r") as json_file:
            try:
                data = json.load(json_file)
            except ValueError:
                data = []
        with open(log_path, "w") as json_file:
            scan_json = {}
            scan_json["scan_name"] = scan_name
            plot_path = plots_dir + "/" + scan_name
            self.color_clusters(show_fig=False, save_plot_path=plot_path)
            scan_json["plot_path"] = plot_path
            scan_json["clusters"] = []
            for cluster_num in range(len(self.anomaly_clusters)):
                # cluster_name = "cluster_" + str(cluster_num)
                # cluster_name = "id_{}_color_{}".format(str(cluster_num),
                #                                        str(round(cluster_num / (len(self.anomaly_clusters) - 1), 2)))
                if (len(self.anomaly_clusters) == 1):
                    cluster_name = "color_{}".format(str(1))
                else:
                    cluster_name = "color_{}".format(str(round(cluster_num / (len(self.anomaly_clusters) - 1), 3)))
                cluster_json = {}
                cluster_json["cluster_name"] = cluster_name
                cluster = self.anomaly_clusters[cluster_num]
                cluster_json["order_keys"] = cluster["order_keys"]
                cluster_json["core_impurities"] = [int(core_imp) for core_imp in cluster["core_impurities"]]
                impurities_and_anomalies = []
                for i in cluster["impurities_inside"]:
                    impurities_and_anomalies.append({"id": int(i), "score": self.anomaly_scores[i]})
                cluster_json["impurities"] = impurities_and_anomalies
                scan_json["clusters"].append(cluster_json)
            data.append(scan_json)
            json.dump(data, json_file)
            json_file.flush()

    def color_clusters(self, show_fig=True, save_plot_path=None):
        blank_image = np.zeros(self.img_shape, np.uint8)
        blank_image[:, :] = (255, 255, 255)

        # tab10 = plt.get_cmap('tab10')
        jet = plt.cm.get_cmap('jet', len(self.anomaly_clusters))
        for impurity in self.indices:
            blank_image[self.markers == impurity + 2] = (0, 0, 0)
        for cluster_id, cluster in enumerate(self.anomaly_clusters):
            if len(self.anomaly_clusters) == 1:
                cluster_color = jet(1)
            else:
                cluster_color = jet(cluster_id / (len(self.anomaly_clusters) - 1))
            for impurity in cluster["impurities_inside"]:
                blank_image[self.markers == impurity + 2] = \
                    (cluster_color[0] * 255, cluster_color[1] * 255, cluster_color[2] * 255)
            # print("cluster id: " + str(cluster_id) + ", mean:" + str(cluster["score"]["mean"]) + ", median:" +
            #       str(cluster["score"]["median"]))

        plt.close()
        matplotlib.rcParams.update({'font.size': 22})
        fig = plt.figure("Area anomaly")
        fig.set_size_inches(30, 20)
        img = plt.imshow(blank_image, cmap='jet')
        if len(self.anomaly_clusters) == 1:
            ticks = [0, 1]
            delta = 0.5
        else:
            ticks = list(np.array(range(len(self.anomaly_clusters))) / (len(self.anomaly_clusters) - 1))
            delta = 0.5 * (1 / (len(self.anomaly_clusters) - 1))

        # bounds = ticks
        # bounds = ticks
        # np.append(bounds, 1)
        # plt.colorbar(img, cmap=jet, boundaries=bounds, ticks=ticks)
        plt.colorbar(img, cmap=jet, ticks=ticks)

        # plt.clim(-delta, 1 + delta)
        plt.clim(0, 1)
        plt.title("Area anomaly")

        if show_fig:
            plt.show()
        elif save_plot_path is not None:
            # plt.savefig(save_plot_path, dpi=fig.dpi)
            plt.savefig(save_plot_path)


def order_clusters(anomaly_clusters_json_file, ordered_clusters_json_file, order_histograms_path=None, order_keys=None,
                   save_ordered_dir="./logs/area/ordered_clusters"):
    if not os.path.exists(order_histograms_path):
        os.makedirs(order_histograms_path)
    if not os.path.exists(save_ordered_dir):
        os.makedirs(save_ordered_dir)
    sorted_clusters_json = []
    with open(anomaly_clusters_json_file, "r") as anomaly_clusters_json:
        data = json.load(anomaly_clusters_json)
        if len(data) == 0 or len(data[0]["clusters"]) == 0:
            return
        if order_keys is None:
            order_keys = [order_key["name"] for order_key in data[0]["clusters"][0]["order_keys"]]
        for i, order_key in enumerate(order_keys):
            clusters_scores = []
            for scan in data:
                for cluster in scan["clusters"]:
                    clusters_scores.append([scan["plot_path"], cluster["cluster_name"],
                                            cluster["order_keys"][i]["score"]])
            # dtype = [('path', str), ('name', str), ('score', float)]
            # clusters_scores = np.array(clusters_scores, dtype=dtype)
            ordered_key = {}
            ordered_key["key_name"] = order_key
            ordered_key["sorted_clusters"] = []
            sorted_clusters = sorted(clusters_scores, key=lambda x: x[2], reverse=True)
            scores_only = np.array(sorted_clusters)[:, 2]
            # print(scores_only.shape)
            scores_only = scores_only.astype(np.float)
            normalized_scores = (scores_only - np.min(scores_only)) / np.ptp(scores_only)
            for cluster_id, cluster in enumerate(sorted_clusters):
                cluster_json = {}
                cluster_json["path"] = cluster[0]
                cluster_json["cluster_name"] = cluster[1]
                cluster_json["score"] = cluster[2]
                cluster_json["norm_score"] = normalized_scores[cluster_id]
                ordered_key["sorted_clusters"].append(cluster_json)
            sorted_clusters_json.append(ordered_key)
            # order_histograms
            if order_histograms_path is not None:
                order_scores = normalized_scores
                fig = plt.figure(order_key)
                plt.hist(order_scores, log=True)
                plt.title(order_key)
                plt.savefig(order_histograms_path+"/"+order_key, dpi=fig.dpi)
    for order in sorted_clusters_json:
        color_sorted_clusters(order["sorted_clusters"], show_fig=False, save_ordered_dir=save_ordered_dir + "/"
                                                                                         + order["key_name"])
    with open(ordered_clusters_json_file, "w") as ordered_json_file:
        json.dump(sorted_clusters_json, ordered_json_file)
    return sorted_clusters


def color_sorted_clusters(sorted_clusters, top_to_show=150, show_fig=True, save_ordered_dir=None):
    if save_ordered_dir is not None:
        if not os.path.exists(save_ordered_dir):
            os.makedirs(save_ordered_dir)
    plt.close()
    for cluster_id, cluster in enumerate(sorted_clusters, start=1):
        if cluster_id > top_to_show:
            break
        bgr_img = cv.imread(cluster["path"])
        img = cv.cvtColor(bgr_img, cv.COLOR_BGR2RGB)
        plt.imshow(img, cmap='jet')
        cluster_name_id = cluster["cluster_name"][cluster["cluster_name"].find("_")+1:]
        plt.title("#" + str(cluster_id) + ": " + cluster_name_id)
                  # + "\n" + str(cluster["score"]))

        if show_fig:
            plt.show()
        elif save_ordered_dir is not None:
            figure = plt.gcf()  # get current figure
            figure.set_size_inches(30, 20)
            plt.savefig(save_ordered_dir+"/"+str(cluster_id)+".png")
